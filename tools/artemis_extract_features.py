import json
import os
import hydra
import torch
import random
import numpy as np

from torch.utils.data import DataLoader
from datasets.caption.transforms import get_transform
from datasets.detection.transforms import *
from hydra import initialize, compose
import h5py
from PIL import Image
from tqdm import tqdm
from einops import rearrange
import argparse
from glob import glob
from torch.utils.data import Dataset, DataLoader, DistributedSampler
import torch.distributed as dist


def collate_fn(batch):
    imgs = [item[0] for item in batch]
    img_ids = [item[1] for item in batch]
    img_idxs = [item[2] for item in batch]
    return imgs, img_ids, img_idxs


class ExtractArtemisDataset(Dataset):

    def __init__(self, root, pre_ann_path, transform=None, overfit=False):
        self.root = root
        self.pre_ann_path = pre_ann_path
        self.transform = transform
        self.overfit = overfit

        data = json.load(open(self.pre_ann_path))
        self.paths_ids = [(os.path.join(self.root, item['image']), item['image_id']) for item in data]
        self.paths_ids = list(set(self.paths_ids))
        if self.overfit:
            self.paths_ids = self.paths_ids[:16000]
        self.img_paths = [pi[0] for pi in self.paths_ids]
        self.img_ids = [pi[1] for pi in self.paths_ids]
        self.img_id2idx = {img_id: img_idx for img_idx, img_id in enumerate(self.img_ids)}

    def __len__(self):
        return len(self.img_paths)

    def __getitem__(self, idx):
        img_path = self.img_paths[idx]
        img_id = self.img_ids[idx]
        img_idx = self.img_id2idx[img_id]

        img = Image.open(os.path.join(self.root, img_path)).convert('RGB')
        if self.transform is not None:
            img = self.transform(img)
        return img, img_id, img_idx


def extract_artemis_features(model, config, device, rank, overfit=False):
    BATCH_SIZE = 64
    model = model.eval()
    print(f"Extract vis feature. Rank: {rank}")
    transform = get_transform(config.dataset.transform_cfg)['valid']
    dataset = ExtractArtemisDataset(
        root=config.dataset.img_root,
        pre_ann_path=config.dataset.pre_ann_path,
        transform=transform,
        overfit=overfit,
    )
    sampler = DistributedSampler(dataset, shuffle=False)
    dataloader = DataLoader(dataset, sampler=sampler, collate_fn=collate_fn, batch_size=(BATCH_SIZE - 1), num_workers=2)

    stage = config.model.grid_stage
    C = config.model.grid_feat_dim
    L = len(dataset)

    if config.dataset.transform_cfg.resize_name in ['normal', 'maxwh']:
        H = config.dataset.transform_cfg.size[0]
        W = config.dataset.transform_cfg.size[1]
    elif config.dataset.transform_cfg.resize_name in ['minmax']:
        H = config.dataset.transform_cfg.size[1]
        W = config.dataset.transform_cfg.size[1]

    fh = H // 64 if stage == -1 else H // 32
    fw = W // 64 if stage == -1 else W // 32

    filename = f"{rank}_" + os.path.basename(config.dataset.hdf5_path)
    dir_path = os.path.dirname(config.dataset.hdf5_path)
    path = os.path.join(dir_path, filename)

    if rank != -1:
        print(f"rank: {rank} - Create hdf5 file: {path}")
        L = len(dataloader) * BATCH_SIZE
        with h5py.File(path, 'w') as h:
            h.create_dataset('image_ids', data=dataset.img_ids)
            h.create_dataset('vis_feat', (L, fh * fw, C), dtype='float32')
            h.create_dataset('vis_mask', (L, 1, 1, fh * fw), dtype='bool')

            if config.model.use_det_feat:
                Q = config.model.detector.det_module.num_queries
                D = config.model.detector.det_module.reduced_dim
                h.create_dataset('det_feat', (L, Q, D), dtype='float32')
                h.create_dataset('det_mask', (L, 1, 1, Q), dtype='bool')
    torch.distributed.barrier()

    with h5py.File(path, 'a') as h:
        vis_features = h['vis_feat']
        vis_masks = h['vis_mask']
        if config.model.use_det_feat:
            det_features = h['det_feat']
            det_masks = h['det_mask']

        tmp_idx = 0
        tmp_ids_list = []
        for imgs, img_ids, _ in tqdm(dataloader, total=len(dataloader)):
            imgs.append(torch.randn(3, H, W))  # random tensor
            imgs = [img.to(device) for img in imgs]

            with torch.no_grad():
                outputs = model(imgs)
                outputs = {k: tensor[:-1].to('cpu').numpy() for k, tensor in outputs.items()}

                for idx, img_id in enumerate(img_ids):
                    vis_features[tmp_idx] = outputs['vis_feat'][idx]
                    vis_masks[tmp_idx] = outputs['vis_mask'][idx]

                    if config.model.use_det_feat:
                        det_features[tmp_idx] = outputs['det_feat'][idx]
                        det_masks[tmp_idx] = outputs['det_mask'][idx]

                    tmp_ids_list.append(img_id)
                    tmp_idx += 1
        h.create_dataset('tmp_ids_list', data=tmp_ids_list)

    torch.distributed.barrier()
    if rank == 0:

        num_gpus = dist.get_world_size()
        with h5py.File(config.dataset.hdf5_path, 'w') as agg_file:
            L = len(dataloader) * BATCH_SIZE * num_gpus
            agg_file.create_dataset('image_ids', data=dataset.img_ids)
            vis_features = agg_file.create_dataset('vis_feat', (L, fh * fw, C), dtype='float32')
            vis_masks = agg_file.create_dataset('vis_mask', (L, 1, 1, fh * fw), dtype='bool')
            if config.model.use_det_feat:
                Q = config.model.detector.det_module.num_queries
                D = config.model.detector.det_module.reduced_dim
                det_features = agg_file.create_dataset('det_feat', (L, Q, D), dtype='float32')
                det_masks = agg_file.create_dataset('det_mask', (L, 1, 1, Q), dtype='bool')

            for r in range(num_gpus):
                filename = f"{r}_" + os.path.basename(config.dataset.hdf5_path)
                dir_path = os.path.dirname(config.dataset.hdf5_path)
                path = os.path.join(dir_path, filename)

                with h5py.File(path, 'r') as f:
                    tmp_ids_list = f['tmp_ids_list'][:len(f['tmp_ids_list'])]

                    for tmp_idx, tmp_id in enumerate(tmp_ids_list):
                        img_idx = dataset.img_id2idx[tmp_id]
                        # Add grid features
                        vis_features[img_idx] = f['vis_feat'][tmp_idx]
                        vis_masks[img_idx] = f['vis_mask'][tmp_idx]

                        # Add det features
                        if config.model.use_det_feat:
                            det_features[img_idx] = f['det_feat'][tmp_idx]
                            det_masks[img_idx] = f['det_mask'][tmp_idx]

                os.remove(path)
                print(f"rank: {rank} - Delete {path}")
        print(f"Saving all to HDF5 file: {config.dataset.hdf5_path}.")
    torch.distributed.barrier()