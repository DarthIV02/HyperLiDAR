from omegaconf import OmegaConf
import torch
import importlib
from models.kpconv.architecture import KPFCNN
import importlib
import argparse
import os
from loader import Loader_Data
from auxiliary.process_data.nuscenes.nuscenes_dataset import DatasetTrainVal, DatasetTest # Change with dataset change
from tqdm import tqdm
import numpy as np
from models.HD.online_hd import OnlineHD
from sklearn.metrics import confusion_matrix
import seaborn as sns
import matplotlib.pyplot as plt
import warnings

def compute_mIoU_torch(preds, labels, num_classes, epoch):
    IoUs = []
    
    for cls in range(1, num_classes):
        "Starts counting from class 0 but labels can be -1"
        # True Positives (TP): Correct predictions for the class
        TP = torch.sum((preds == cls) & (labels == cls)).float()
        
        # False Positives (FP): Incorrectly predicted as the class
        FP = torch.sum((preds == cls) & (labels != cls)).float()
        
        # False Negatives (FN): Actual class, but not predicted correctly
        FN = torch.sum((preds != cls) & (labels == cls)).float()
        
        denominator = TP + FP + FN
        
        if denominator == 0:
            IoU = torch.tensor(1.0)  # If no pixels are present for this class, treat IoU as 1
        else:
            IoU = TP / denominator
            IoUs.append(IoU)
    
    per_class_IoUs = torch.tensor(IoUs)
    mIoU = torch.mean(per_class_IoUs)  # Mean IoU

    # Compute the confusion matrix
    cm = confusion_matrix(labels.cpu(), preds.cpu())

    plt.figure(figsize=(8, 6))
    sns.heatmap(cm, annot=True, fmt='d', cmap='Blues', xticklabels=range(num_classes), yticklabels=range(num_classes))
    plt.xlabel('Predicted Label')
    plt.ylabel('True Label')
    plt.title('Confusion Matrix')
    plt.savefig(f'./results/epoch_{epoch}.png')  # Save the figure to a file
    plt.close()  # Close the plot to free up memory

    
    return mIoU, per_class_IoUs

warnings.filterwarnings("ignore")

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
model_information.num_classes = 19 # Change with dataset change
model_information.ignore_label = -1
model_information.in_features_dim = 1
from models.kpconv_model import SemanticSegmentationModel
module = importlib.import_module('models.kpconv.architecture')
model_type = getattr(module, "KPFCNN")
semantic_model = SemanticSegmentationModel(model_information,cfg,model_type)

lbl_values = [i for i in range(19)] # Change with dataset change
ign_lbls = [-1]
model = KPFCNN(model_information, lbl_values, ign_lbls)
model.to(device)
model.load_state_dict(torch.load(os.path.join(cfg.logger.save_path, cfg.logger.model_name)))

# Data Load
filelist_train = [os.path.join(cfg.target_path, 'train_pointclouds', fname) for fname in os.listdir(os.path.join(cfg.target_path, 'train_pointclouds')) if os.path.splitext(fname)[1]==".npy"]
filelist_train.sort()
filelist_val = filelist_train[:3]
filelist_train = filelist_train[3:]

print("Creating dataloader...", flush=True)
ds = DatasetTrainVal(filelist_train, os.path.join(cfg.target_path, 'train_pointclouds'),
                            training=True,
                            npoints=cfg.npoints,
                            iteration_number=(cfg.batchsize*cfg.trainer.epoch)*10,
                            jitter=cfg.jitter)
train_loader = torch.utils.data.DataLoader(ds, batch_size=1, shuffle=False, # Change batch_size
                                    num_workers=1)

#ds_val = DatasetTrainVal("Paris.ply", os.path.join('/root/main/dataset/', 'test_10_classes'))
#val_loader = torch.utils.data.DataLoader(ds_val, batch_size=cfg.batchsize, shuffle=False,
#                                    num_workers=cfg.threads)

ds_val = DatasetTrainVal(filelist_val, os.path.join(cfg.target_path, 'train_pointclouds'), # Uncomment for validation
                            training=False,
                            npoints=cfg.npoints,
                            iteration_number=cfg.batchsize*cfg.trainer.epoch*5,
                            jitter=0)
val_loader = torch.utils.data.DataLoader(ds_val, batch_size=1, shuffle=False,
                                    num_workers=cfg.threads)

hd_model = OnlineHD(cfg.n_features, cfg.n_dimensions, cfg.n_classes, cfg, model, device=device)

for epoch in range(0, cfg.trainer.epoch):

    t = tqdm(train_loader, ncols=100, desc="Train Epoch {}".format(epoch), disable=False)
    for data in t:
        pts = data['pts']#.to(device)
        features = data['features']#.to(device)
        seg = data['target']#.to(device)
        #print(seg)
        #pointcloud = np.hstack((pts.reshape((cfg.batchsize, 3, pts.shape[2])), np.zeros((cfg.batchsize, 1, pts.shape[2])), (seg-1).reshape((cfg.batchsize, 1, seg.shape[1])),np.zeros((cfg.batchsize, 1, pts.shape[2]))))
        pointcloud = np.concatenate((
            pts.reshape((1, pts.shape[2], 3)), # Change batch_size
            np.zeros((1, pts.shape[2], 1)), # Change batch_size
            (seg - 1).reshape((1, seg.shape[1], 1)), # Change batch_size
            np.zeros((1, pts.shape[2], 1)) # Change batch_size
        ), axis=2)
        #print("Pointcloud:", pointcloud.shape)
        r_clouds, r_inds_list = semantic_model.prepare_data(pointcloud,False,True)
        labels = r_clouds.labels.clone().detach()
        if len(cfg.bundle) > 1:
            x_append = {}
            for stop in cfg.bundle:
                x = hd_model.feature_extractor(r_clouds, stop)
                x_append[stop] = x
            hd_model.fit(samples=x_append, labels=labels, points=r_clouds.points)
        else:
            x = hd_model.feature_extractor(r_clouds, cfg.hd_block_stop)
            hd_model.fit(samples=x, labels=labels)
        
        # Free the memory
        del r_clouds
        del x
        torch.cuda.empty_cache()

    # Validation

    t_val = tqdm(val_loader, ncols=100, desc="Val Epoch {}".format(epoch), disable=False)
    preds_total = torch.zeros((ds_val.__len__(), 1*ds_val.npoints)).to(device) #cfg.batchsize
    labels = torch.zeros((ds_val.__len__(), 1*ds_val.npoints)).to(device) #cfg.batchsize
    for i, data_val in enumerate(t_val):
        pts = data_val['pts']#.to(device)
        features = data_val['features']#.to(device)
        seg = data_val['target']#.to(device)

        #print(torch.bincount(seg.to(torch.int)))
        total_num_points = pts.shape[2]*1 # Change batch_size
        labels[i] = seg.reshape((pts.shape[2]*1))-1 # Reshaping flat to an array of single dimension # Change batch_size

        pointcloud = np.concatenate((
            pts.reshape((1, total_num_points, 3)), # Change batch_size
            np.zeros((1, total_num_points, 1)), # Change batch_size
            np.zeros((1, total_num_points, 1)) -1, # Change batch_size
            np.zeros((1, total_num_points, 1)) # Change batch_size
        ), axis=2)
        r_clouds, r_inds_list = semantic_model.prepare_data(pointcloud,False,True)
        #print("r_clouds: ", r_clouds.points[0].shape)
        #print("r_clouds: ", r_clouds.labels.shape)
        #print("r_inds_list len: ", len(r_inds_list))
        #print("r_inds_list shape: ", r_inds_list[0].shape)
        #print("r_inds_list: ", max(r_inds_list[0]))
        y = hd_model.forward(r_clouds)
        #print("y: ", y.shape)
        preds = np.zeros((total_num_points))
        preds = y[r_inds_list[0]]
        #preds = preds.reshape((cfg.batchsize, pts.shape[2]))
        preds_total[i] = preds
        #L = L+total_num_points

    mIoU, per_class_iou = compute_mIoU_torch(preds_total.view(-1)+1, labels.view(-1)+1, cfg.n_classes+1, epoch) # Change when more datasets
    print(f"Val mIoU in epoch {epoch}: ", mIoU, per_class_iou)
    print(torch.bincount((labels+1).view(-1).to(torch.int)))
    




    