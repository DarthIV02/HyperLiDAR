from omegaconf import OmegaConf
import torch
import importlib
from models.kpconv.architecture import KPFCNN
import importlib
import argparse
import os
from loader import Loader_Data
from auxiliary.process_data.npm3d.npm3d_dataset import DatasetTrainVal as Dataset
from tqdm import tqdm
import numpy as np

parser = argparse.ArgumentParser()
parser.add_argument('-cfg', '--config', help='the path to the setup config file', default='cfg/args.yaml')
args = parser.parse_args()

cfg = OmegaConf.load(args.config)

if torch.cuda.is_available():
    device = torch.device("cuda:0")
else:
    device = torch.device("cpu")

module = importlib.import_module('models.kpconv.kpconv')
model_information = getattr(module, "KPFCNN")()
model_information.num_classes = 16
model_information.ignore_label = -1
model_information.in_features_dim = 1
from models.kpconv_model import SemanticSegmentationModel
module = importlib.import_module('models.kpconv.architecture')
model_type = getattr(module, "KPFCNN")
semantic_model = SemanticSegmentationModel(model_information,cfg,model_type)

lbl_values = [i for i in range(16)]
ign_lbls = [-1]
model = KPFCNN(model_information, lbl_values, ign_lbls)
model.to(device)
model.load_state_dict(torch.load(os.path.join(cfg.logger.save_path, cfg.logger.model_name)))

# Data Load
filelist_train = [os.path.join(cfg.target_path, 'train_pointclouds', fname) for fname in os.listdir(os.path.join(cfg.target_path, 'train_pointclouds')) if os.path.splitext(fname)[1]==".npy"]
filelist_train.sort()
filelist_val=filelist_train[-2:]

print("Creating dataloader...", flush=True)
ds = Dataset(filelist_train, os.path.join(cfg.target_path, 'train_pointclouds'),
                            training=True,
                            npoints=cfg.npoints,
                            iteration_number=cfg.batchsize*cfg.trainer.epoch,
                            jitter=cfg.jitter)
train_loader = torch.utils.data.DataLoader(ds, batch_size=cfg.batchsize, shuffle=True,
                                    num_workers=cfg.threads)

ds_val = Dataset(filelist_val, os.path.join(cfg.target_path, 'train_pointclouds'),
                            training=True,
                            npoints=cfg.npoints,
                            iteration_number=cfg.batchsize*cfg.trainer.epoch,
                            jitter=cfg.jitter)
val_loader = torch.utils.data.DataLoader(ds_val, batch_size=cfg.batchsize, shuffle=True,
                                    num_workers=cfg.threads)

for epoch in range(0, cfg.trainer.epoch):
    pointclouds = []
    t = tqdm(train_loader, ncols=100, desc="Train Epoch {}".format(epoch), disable=False)
    for data in t:
        pts = data['pts']#.to(device)
        print(pts.shape)
        print(np.zeros(pts.shape[2]).reshape(-1,1).shape)
        print((seg-1).shape)
        print(np.zeros(pts.shape[2]).reshape(-1,1).shape)
        features = data['features']#.to(device)
        seg = data['target']#.to(device)
        pointcloud = np.hstack((pts, np.zeros(pts.shape[2]).reshape(-1,1), seg-1,np.zeros(pts.shape[2]).reshape(-1,1)))
        pointcloud = pointcloud.to(device)
    
    t_val = tqdm(val_loader, ncols=100, desc="Val Epoch {}".format(epoch), disable=False)
    for data_val in t_val:
        pts = data_val['pts'].to(device)
        print(pts.shape)
        features = data_val['features'].to(device)
        seg = data_val['target'].to(device)
        print(seg.shape)

    