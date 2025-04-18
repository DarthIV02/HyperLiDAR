
import torch
import numpy as np
import os
import random
from torchvision import transforms
from PIL import Image
from tqdm import *
from nuscenes.nuscenes import NuScenes
from nuscenes.utils.data_classes import LidarPointCloud
from nuscenes.utils.data_io import load_bin_file
from omegaconf import OmegaConf

def rotate_point_cloud_z(batch_data):
    """ Randomly rotate the point clouds to augument the dataset
        rotation is per shape based along up direction
        Input:
          BxNx3 array, original batch of point clouds
        Return:
          BxNx3 array, rotated batch of point clouds
    """
    rotation_angle = np.random.uniform() * 2 * np.pi
    cosval = np.cos(rotation_angle)
    sinval = np.sin(rotation_angle)
    rotation_matrix = np.array([[cosval, sinval, 0],
                                [-sinval, cosval, 0],
                                [0, 0, 1],])
    return np.dot(batch_data, rotation_matrix)

# Part dataset only for training / validation
class DatasetTrainVal():

    def compute_mask(self, xyzrgb, pt, bs):
        # build the mask
        mask_x = np.logical_and(xyzrgb[:,0]<=pt[0]+bs/2, xyzrgb[:,0]>=pt[0]-bs/2)
        mask_y = np.logical_and(xyzrgb[:,1]<=pt[1]+bs/2, xyzrgb[:,1]>=pt[1]-bs/2)
        mask = np.logical_and(mask_x, mask_y)
        return mask


    def __init__ (self, filelist, folder,
                    training=False, 
                    block_size=8,
                    npoints = 8192,
                    jitter = 0,
                    iteration_number = None,
                    rgb_dropout=False,
                    rgb=True,
                    in_memory=True, 
                    network_function=None):

        self.filelist = filelist
        self.folder = folder
        self.training = training
        self.bs = block_size
        self.npoints = npoints
        self.iterations = iteration_number
        self.rgb_dropout = rgb_dropout
        self.rgb=rgb
        self.in_memory = in_memory
        self.jitter = jitter

        self.transform = transforms.ColorJitter(
            brightness=jitter,
            contrast=jitter,
            saturation=jitter)

        if network_function is not None:
            self.net = network_function()
        else:
            self.net = None

        if self.in_memory:
            self.data = []
            for filename in filelist:
                data = np.load(os.path.join(self.folder, filename))
                self.data.append(data)
        print("Len:", len(self.filelist)-1)

    #@with_indices_computation_rotation
    def __getitem__(self, index):

        # load the data
        index = random.randint(0, len(self.filelist)-1)
        #print(index)
        if self.in_memory:
            pts = self.data[index]
        else:
            pts = np.load(os.path.join(self.folder, self.filelist[index]))
        
        # get the features
        fts = np.tile(pts[:,3], [3,1]).transpose()
        # get the labels
        lbs = pts[:,4].astype(int) # the generation script label starts at 1
        #print("labels dataloader 1:", np.bincount(lbs))
        # get the point coordinates
        pts = pts[:, :3]
        
        # random selection
        if pts.shape[0] < self.npoints:
            choice = np.random.choice(pts.shape[0], self.npoints, replace=True)
        else:
            choice = np.random.choice(pts.shape[0], self.npoints, replace=False)
        pts = pts[choice]
        lbs = lbs[choice]
        #print("labels dataloader 3:", np.bincount(lbs))
        fts = fts[choice]

        source_data_cfg = OmegaConf.load("./auxiliary/process_data/nuscenes/nuscenes_mini.yaml")
        lbs = np.array([source_data_cfg.learning_map[int(i)] for i in lbs])
        #print("labels dataloader 4:", np.bincount(lbs))

        # data augmentation
        if self.training:
            # random rotation
            pts = rotate_point_cloud_z(pts)

            # random jittering
            fts = fts.astype(np.uint8)
            fts = np.array(self.transform( Image.fromarray(np.expand_dims(fts, 0)) ))
            fts = np.squeeze(fts, 0)

        fts = fts.astype(np.float32)
        fts = fts/255 - 0.5

        pts = torch.from_numpy(pts).float()
        fts = torch.from_numpy(fts).float()
        lbs = torch.from_numpy(lbs).long()

        if (not self.rgb) or (self.training and self.rgb_dropout and random.randint(0,1)):
            fts = torch.ones(fts.shape).float()

        pts = pts.transpose(0,1)
        fts = fts.transpose(0,1)

        return_dict = {
            "pts": pts,
            "features": fts,
            "target": lbs,
        }

        return return_dict

    def __len__(self):
        return self.iterations

class DatasetTest():

    def compute_mask(self, xyzrgb, pt, bs):
        # build the mask
        mask_x = np.logical_and(xyzrgb[:,0]<=pt[0]+bs/2, xyzrgb[:,0]>=pt[0]-bs/2)
        mask_y = np.logical_and(xyzrgb[:,1]<=pt[1]+bs/2, xyzrgb[:,1]>=pt[1]-bs/2)
        mask = np.logical_and(mask_x, mask_y)
        return mask

    def __init__ (self, filename, folder,
                    block_size=8,
                    npoints = 8192, rgb=True,
                    network_function=None, step=None, offset=0):

        self.folder = folder
        self.bs = block_size
        self.npoints = npoints
        self.filename = filename
        self.rgb=rgb

        step = block_size if step is None else step
        
        if network_function is not None:
            self.net = network_function()
        else:
            self.net = None

        nusc = NuScenes(version='v1.0-mini', dataroot=folder, verbose=True)


        # get the data
        sample = nusc.get('sample',filename)
        sample_data = sample['data']['LIDAR_TOP']
        record = nusc.get('sample_data', sample_data)
        pcl_path = os.path.join(nusc.dataroot, record['filename'])
        pc = LidarPointCloud.from_file(pcl_path)
        points = pc.points
        x = points[0]
        y = points[1]
        z = points[2]
        reflectance = points[3]
        label_file = os.path.join(nusc.dataroot, nusc.get('lidarseg',sample_data)['filename'])
        label = load_bin_file(label_file, type='lidarseg')
        self.xyzrgb = np.stack([x,y,z,reflectance], axis=1).astype(np.float32)

        mini = np.around(self.xyzrgb[:,:2].min(0), decimals=4)
        discretized = np.around(((self.xyzrgb[:,:2]-mini+offset).astype(float)/step), decimals=2)#.astype(int)
        self.pts = np.unique(discretized, axis=0)
        self.pts = self.pts.astype(np.float)*step + mini - offset + step/2

        # compute the masks
        self.choices = []
        self.pts_ref = []
        for index in tqdm(range(self.pts.shape[0]), ncols=80):
            pt_ref = self.pts[index]
            mask = self.compute_mask(self.xyzrgb, pt_ref, self.bs)

            pillar_points_indices = np.where(mask)[0]
            valid_points_indices = pillar_points_indices.copy()

            while(valid_points_indices is not None):
                # print(valid_points_indices.shape[0])
                if valid_points_indices.shape[0] > self.npoints:
                    choice = np.random.choice(valid_points_indices.shape[0], self.npoints, replace=True)
                    mask[valid_points_indices[choice]] = False
                    choice = valid_points_indices[choice]
                    valid_points_indices = np.where(mask)[0]
                else:
                    choice = np.random.choice(pillar_points_indices.shape[0], self.npoints-valid_points_indices.shape[0], replace=True)
                    choice = np.concatenate([valid_points_indices, pillar_points_indices[choice]], axis=0)
                    valid_points_indices = None

                self.choices.append(choice)
                self.pts_ref.append(pt_ref)

    
    #@with_indices_computation_rotation
    def __getitem__(self, index):
        
        choice = self.choices[index]
        pts = self.xyzrgb[choice]
        pt_ref = self.pts_ref[index]

        # get the features 
        fts = np.tile(pts[:,3], [3,1]).transpose()
        fts = fts.astype(np.float32) / 255 - 0.5
        # get the point coordinates
        pts = pts[:,:3]


        # go numpy        
        pts = torch.from_numpy(pts).float()
        fts = torch.from_numpy(fts).float()
        choice = torch.from_numpy(choice).long()

        if not self.rgb:
            fts = torch.ones(fts.shape).float()

        # transpose for light conv point
        pts = pts.transpose(0,1)
        fts = fts.transpose(0,1)

        return_dict = {
            "pts": pts,
            "features": fts,
            "pts_ids": choice
        }

        return return_dict

    def __len__(self):
        return len(self.choices)



