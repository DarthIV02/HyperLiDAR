from models.waffleiron.segmenter import Segmenter
import torch
from datasets import LIST_DATASETS, Collate
from tqdm import tqdm
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision
from collections import OrderedDict
import warnings
import copy
import random
import numpy as np
import os

import argparse
import wandb
from torchmetrics.classification import MulticlassJaccardIndex
import torchmetrics
from tqdm import tqdm
from sklearn.metrics import confusion_matrix

import torchhd
from torchhd.models import Centroid
from torchhd import embeddings

class Encoder(nn.Module):
    def __init__(self, hd_dim, size):
        super(Encoder, self).__init__()
        self.flatten = torch.nn.Flatten()
        self.projection = embeddings.Projection(size, hd_dim)
        self.projection.weight = nn.Parameter(torchhd.normalize(self.projection.weight), requires_grad=False) # Binary

    def forward(self, x):
        sample_hv = self.projection(x)
        return torchhd.hard_quantize(sample_hv)

class Feature_Extractor:
    def __init__(self, input_channels=5, feat_channels=768, depth=48, 
                 grid_shape=[[256, 256], [256, 32], [256, 32]], nb_class=16, layer_norm=True, 
                 device=torch.device("cpu"), early_exit = 48):
        self.model = Segmenter(
            input_channels=input_channels,
            feat_channels=feat_channels,
            depth=depth,
            grid_shape=grid_shape,
            nb_class=nb_class, # class for prediction
            #drop_path_prob=config["waffleiron"]["drop_path"],
            layer_norm=layer_norm,
        )

        classif = torch.nn.Conv1d(
            feat_channels, nb_class, 1
        )
        torch.nn.init.constant_(classif.bias, 0)
        torch.nn.init.constant_(classif.weight, 0)
        self.model.classif = torch.nn.Sequential(
            torch.nn.BatchNorm1d(feat_channels),
            classif,
        )

        for p in self.model.parameters():
            p.requires_grad = False
        for p in self.model.classif.parameters():
            p.requires_grad = True

        def get_optimizer(parameters):
            return torch.optim.AdamW(
                parameters,
                lr=0.001,
                weight_decay=0.003,
            )

        optim = get_optimizer(self.model.parameters())
        self.device = device
        self.device_string = "cuda:0" if torch.cuda.is_available() else "cpu"
        self.num_classes = nb_class
        self.early_exit = early_exit
    
    def load_pretrained(self, path):
        # Load pretrained model
        path_to_ckpt = path
        checkpoint = torch.load(path_to_ckpt,
            map_location=device_string)
        state_dict = checkpoint["net"]  # Adjust key as needed
        new_state_dict = OrderedDict()

        for k, v in state_dict.items():
            new_key = k.replace("module.", "")  # Remove "module." prefix
            new_state_dict[new_key] = v

        self.model.load_state_dict(new_state_dict)

        print(
            f"Checkpoint loaded on {self.device_string}: {path_to_ckpt}"
        )

        if self.device_string != 'cpu':
            torch.cuda.set_device(self.device_string) # cuda:0
            self.model = self.model.cuda(self.device_string) # cuda:0

        self.model.eval()

    def forward_model(self, it, batch):
        feat = batch["feat"]
        labels = batch["labels_orig"]
        cell_ind = batch["cell_ind"]
        occupied_cell = batch["occupied_cells"]
        neighbors_emb = batch["neighbors_emb"]
        if device_string != 'cpu':
            feat = feat.cuda(0, non_blocking=True)
            labels = labels.cuda(0, non_blocking=True)
            batch["upsample"] = [
                up.cuda(0, non_blocking=True) for up in batch["upsample"]
            ]
            cell_ind = cell_ind.cuda(0, non_blocking=True)
            occupied_cell = occupied_cell.cuda(0, non_blocking=True)
            neighbors_emb = neighbors_emb.cuda(0, non_blocking=True)
        net_inputs = (feat, cell_ind, occupied_cell, neighbors_emb)

        if device_string != 'cpu':
            with torch.autocast("cuda", enabled=True):
                # Logits
                with torch.no_grad():
                    out = self.model(*net_inputs, self.early_exit)
                    encode, tokens, out = out[0], out[1], out[2]
                    pred_label = out.max(1)[1]

                    # Only return samples that are not noise
                    torch.cuda.synchronize(device=self.device)
                    where = labels != 255
                    torch.cuda.synchronize(device=self.device)
        else:
            with torch.no_grad():
                out = self.model(*net_inputs, self.early_exit)
                encode, tokens, out = out[0], out[1], out[2]
                pred_label = out.max(1)[1]

                # Only return samples that are not noise
                where = labels != 255
        
        return tokens[0,:,where], labels[where], pred_label[0, where]

    def test(self, loader, total_voxels):        
        # Metric
        miou = MulticlassJaccardIndex(num_classes=self.num_classes, average=None).to(self.device, non_blocking=True)
        final_labels = torch.empty((total_voxels), device=self.device)
        final_pred = torch.empty((total_voxels), device=self.device)
        
        start_idx = 0
        for it, batch in tqdm(enumerate(loader), desc="SoA testing"):
            features, labels, soa_result = self.forward_model(it, batch, self.early_exit)
            shape_sample = labels.shape[0]
            labels = labels.to(dtype = torch.int64, device = self.device, non_blocking=True)
            soa_result = soa_result.to(device=self.device, non_blocking=True)
            final_labels[start_idx:start_idx+shape_sample] = labels

            final_pred[start_idx:start_idx+shape_sample] = soa_result

            start_idx += shape_sample

        final_labels = final_labels[:start_idx]
        final_pred = final_pred[:start_idx]

        print("================================")

        print('Pred FE', final_pred, "\tShape: ", final_pred.shape)
        print('Label', final_labels, "\tShape: ", final_labels.shape)
        accuracy = miou(final_pred, final_labels)
        avg_acc = torch.mean(accuracy)
        print(f'accuracy: {accuracy}')
        print(f'avg acc: {avg_acc}')

        #cm = confusion_matrix(pred_hd, first_label, labels=torch.Tensor(range(0,15)))
        #print("Confusion matrix \n")
        #print(cm)

        print("================================")

class HD_Model:
    def __init__(self, in_dim, out_dim, num_classes, path_pretrained, 
                 device=torch.device("cpu"), **kwargs):

        encode = Encoder(out_dim, in_dim)
        self.encode = encode.to(device=device, non_blocking=True)

        model = Centroid(out_dim, num_classes)
        self.model = model.to(device=device, non_blocking=True)
        self.device = device
        self.feature_extractor = Feature_Extractor(nb_class = num_classes, device=self.device, early_exit=kwargs['args'].layers)
        self.feature_extractor.load_pretrained(path_pretrained)
        self.stop = kwargs['args'].layers
        self.num_classes = num_classes
        self.kwargs = kwargs

    def normalize(self, samples):

        """ Normalize with Z-score"""

        mean = torch.mean(samples, dim=0)
        std = torch.std(samples, dim=0)

        samples = (samples - mean) / (std + 1e-8)

        return samples

    def set_loaders(self, train_loader, val_loader):
        self.train_loader = train_loader
        self.val_loader = val_loader
        self.num_vox_train = 0
        self.num_vox_val = 0

        for batch in self.train_loader:
            labels = batch["labels_orig"]
            labels = labels.cuda(0, non_blocking=True)
            torch.cuda.synchronize(device=self.device)
            where = labels != 255
            torch.cuda.synchronize(device=self.device)
            self.num_vox_train += labels[where].shape[0]

        for batch in self.val_loader:
            labels = batch["labels_orig"]
            labels = labels.cuda(0, non_blocking=True)
            torch.cuda.synchronize(device=self.device)
            where = labels != 255
            torch.cuda.synchronize(device=self.device)
            self.num_vox_val += labels[where].shape[0]

        print("Finished loading data loaders")
    
    def sample_to_encode(self, it, batch):
        features, labels, soa_result = self.feature_extractor.forward_model(it, batch)
        features = torch.transpose(features, 0, 1).to(dtype=torch.float32, device = self.device, non_blocking=True)
        labels = labels.to(dtype=torch.int64, device = self.device, non_blocking=True)

        features = self.normalize(features) # Z1 score seems to work

        # HD training
        samples_hv = self.encode(features)

        return samples_hv, labels
    
    def train(self):

        """ Initial training pass """

        print("\nTrain First\n")

        for it, batch in tqdm(enumerate(self.train_loader), desc="Training"):
           
            samples_hv, labels = self.sample_to_encode(it, batch)
            #samples_hv = samples_hv.reshape((1,samples_hv.shape[0]))
            if self.kwargs['args'].add_lr:
                samples_per_class = torch.bincount(labels, minlength=self.num_classes)
                inverse_weights = 1.0 / (samples_per_class + 1.0)
    
                # Normalize the weights to sum to 1
                normalized_weights = inverse_weights / torch.sum(inverse_weights)
                #print(normalized_weights)

                for c in range(self.num_classes):
                    if samples_per_class[c] > 0:
                        #samples_hv = samples_hv.reshape((1,samples_hv.shape[0]))
                        here = labels == c
                        self.model.add(samples_hv[here], labels[here], lr=normalized_weights[c])
            else:
                self.model.add(samples_hv, labels)
            
            self.model.weight = nn.Parameter(torchhd.normalize(self.model.weight), requires_grad=False) # Binary
            torch.cuda.synchronize(device=self.device)

    def retrain(self, epochs):
        
        """ Retrain with misclassified samples (also substract)"""
        
        for e in tqdm(range(epochs), desc="Epoch"):
            #count = 0

            for it, batch in tqdm(enumerate(self.train_loader), desc=f"Retraining epoch {e}"):
                
                samples_hv, labels = self.sample_to_encode(it, batch)
                sim = self.model(samples_hv, dot=True)
                #pred_hd = sim.argmax(1).data
                pred_hd = torch.argmax(sim, axis=1)

                is_wrong = labels != pred_hd

                # cancel update if all predictions were correct
                if is_wrong.sum().item() == 0:
                    continue

                # only update wrongly predicted inputs
                samples_hv = samples_hv[is_wrong]
                labels = labels[is_wrong]
                pred_hd = pred_hd[is_wrong]

                #count = labels.shape[0]

                self.model.weight.index_add_(0, labels, samples_hv)
                self.model.weight.index_add_(0, pred_hd, samples_hv, alpha=-1.0)

                torch.cuda.synchronize(device=self.device)

            #print(f"Misclassified for {it}: ", count)

            # If you want to test for each sample
            self.test_hd()

    def test_hd(self, loader='val'):

        """ Testing over all the samples in all the scans given """

        if loader == 'val':
            loader = self.val_loader
            num_vox = self.num_vox_val
        else:
            loader = self.train_loader
            num_vox = self.num_vox_train
        
        # Metric
        miou = MulticlassJaccardIndex(num_classes=self.num_classes, average=None).to(self.device, non_blocking=True)
        final_labels = torch.empty((num_vox+1000), dtype=torch.int64, device=self.device)
        final_pred = torch.empty((num_vox+1000), dtype=torch.int64, device=self.device)
        
        start_idx = 0
        for it, batch in tqdm(enumerate(loader), desc="Validation:"):
            
            samples_hv, labels = self.sample_to_encode(it, batch)
            torch.cuda.synchronize(device=self.device)
            
            shape_sample = labels.shape[0]

            #pred_hd = self.model(samples_hv, dot=True).argmax(1).data
            sim = self.model(samples_hv, dot=True)
            torch.cuda.synchronize(device=self.device)

            pred_hd = torch.argmax(sim, axis=1)
            torch.cuda.synchronize(device=self.device)

            #print("Labels: ", labels.shape[0])
            #print(start_idx, start_idx+shape_sample)
            #print(shape_sample)

            final_labels[start_idx:start_idx+shape_sample] = labels
            final_pred[start_idx:start_idx+shape_sample] = pred_hd

            start_idx += shape_sample

        final_labels = final_labels[:start_idx]
        final_pred = final_pred[:start_idx]

        print("================================")

        #print('pred_ts', pred_ts)
        print('pred_hd', final_pred, "\tShape: ", final_pred.shape)
        print('label', final_labels, "\tShape: ", final_labels.shape)
        accuracy = miou(final_pred, final_labels)
        avg_acc = torch.mean(accuracy)
        print(f'accuracy: {accuracy}')
        print(f'avg acc: {avg_acc}')

        log_data = {f"Training class_{i}_IoU": c for i, c in enumerate(accuracy)}
        log_data["Retraining epoch"] = avg_acc
        wandb.log(log_data)

        #cm = confusion_matrix(pred_hd, first_label, labels=torch.Tensor(range(0,15)))
        #print("Confusion matrix \n")
        #print(cm)

        print("================================")

def parse_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument('-stop', '--layers', type=int, help='how many layers deep', default=48)
    #parser.add_argument('-soa', '--soa', action="store_true", default=False, help='Plot SOA')
    parser.add_argument('-number_samples', '--number_samples', type=int, help='how many scans to train', default=10000)
    parser.add_argument(
            "--seed", default=None, type=int, help="Seed for initializing training"
        )
    parser.add_argument(
            "--add_lr", action="store_true", default=False, help='Add lr to help class imbalance'
        )
    parser.add_argument(
            "--dataset", choices=['nuscenes', 'semantic_kitti', 'tls'], default='nuscenes', help='Which dataset to train and test on?'
        )
    
    # HD arguments
    parser.add_argument('--dim', type=int, help='Dimensionality of Hypervectors', default=10000)
    #parser.add_argument('-val', '--val', action="store_true", default=False, help='Train with validation for each scan')
    args = parser.parse_args()
    return args

if __name__ == "__main__":
    
    args = parse_arguments()

    # Set seed
    if args.seed is not None:
        random.seed(args.seed)
        np.random.seed(args.seed)
        torch.manual_seed(args.seed)
        torch.cuda.manual_seed(args.seed)
        os.environ["PYTHONHASHSEED"] = str(args.seed)

    DIMENSIONS = args.dim
    FEAT_SIZE = 768
    NUM_LEVELS = 8000
    BATCH_SIZE = 1  # for GPUs with enough memory we can process multiple images at ones
    num_classes = 16
    path_pretrained = '/root/main/ScaLR/saved_models/ckpt_last_scalr.pth'

    wandb.login(key="9487c04b8eff0c16cac4e785f2b57c3a475767d3")

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print("Using {} device".format(device))
    device_string = "cuda:0" if torch.cuda.is_available() else "cpu"

    # Modify the path for each of the folders

    if args.dataset == 'nuscenes':
        path = '/root/main/dataset/nuscenes'
    elif args.dataset == 'semantic_kitti':
        path = '/root/main/dataset/semantickitti'
    elif args.dataset == 'tls':
        path = '/root/main/dataset/tls'

    kwargs = {
        "rootdir": path,
        "input_feat": ["intensity", "xyz", "radius"],
        "voxel_size": 0.1,
        "num_neighbors": 16,
        "dim_proj": [2, 1, 0],
        "grids_shape": [[256, 256], [256, 32], [256, 32]],
        "fov_xyz": [[-64, -64, -8], [64, 64, 8]], # Check here
    }

    # Get datatset
    DATASET = LIST_DATASETS.get(args.dataset)

    # Train dataset
    dataset = DATASET(
        phase="train",
        **kwargs,
    )
    #print(dataset.voxel_size)
    dataset_train = copy.deepcopy(dataset)
    dataset_val = copy.deepcopy(dataset)
    dataset_train.init_training()
    dataset_val.init_val()

    train_loader = torch.utils.data.DataLoader(
            dataset_train,
            batch_size=1,
            pin_memory=True,
            drop_last=True,
            collate_fn=Collate(device=device),
            persistent_workers=False,
        )

    val_loader = torch.utils.data.DataLoader(
            dataset_val,
            batch_size=1,
            pin_memory=True,
            drop_last=True,
            collate_fn=Collate(device=device),
            persistent_workers=False,
        )

    hd_model = HD_Model(FEAT_SIZE, DIMENSIONS, num_classes, path_pretrained, device=device, args=args)
    hd_model.set_loaders(train_loader=train_loader, val_loader=val_loader)

    run = wandb.init(
        # Set the project where this run will be logged
        project="scalr_hd",
        # Track hyperparameters and run metadata
        config={
            "encoding": "Random Projection",
            "hd_dim": 10000,
            "training_samples":404,
        },
        id=f"early_exit_complete_{args.layers}_norm_dim_{DIMENSIONS}",
    )


    ####### HD Pipeline ##########

    print("Initial Training")
    hd_model.train()

    print("Testing")
    hd_model.test_hd()

    print("Retraining")
    hd_model.retrain(epochs=10)
    
    print("Testing")
    hd_model.test_hd()

    

    ####### SOA results ##########
    #print("SoA results")

    #hd_model.feature_extractor.test(hd_model.val_loader, hd_model.num_vox_val+1000, 48)
