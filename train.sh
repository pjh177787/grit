export DATA_ROOT=/home/dev/Git/coco-2014
# with pretrained object detector on 4 datasets
python train_caption.py \
    exp.name=caption_4ds \
    model.detector.checkpoint=/home/dev/Git/grit/ckpt/detector_checkpoint_4ds.pth

# with pretrained object detector on Visual Genome
# python train_caption.py exp.name=caption_4ds model.detector.checkpoint=vg_detector_path