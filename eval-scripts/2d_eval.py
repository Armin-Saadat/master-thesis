# ////////////////////////////////////////// imports ///////////////////////////////////////
import os, sys
import glob
import time
import numpy as np
import shutil
import imageio
import pickle
import random
import torch
import torch.nn as nn
import torch.nn.functional as nnf
from torch.utils.data import Dataset, DataLoader
import matplotlib.pyplot as plt
from skimage.transform import resize

os.environ['VXM_BACKEND'] = 'pytorch'
os.environ['NEURITE_BACKEND'] = 'pytorch'
import voxelmorph as vxm
import neurite as ne

device = 'cuda'
os.environ['CUDA_VISIBLE_DEVICES'] = '0'
torch.backends.cudnn.deterministic = True


# ////////////////////////////////////////// load & normalize ///////////////////////////////////////

labeled_images = np.load('/home/adeleh/MICCAI-2022/UMIS-data/medical-data/synaps/labeled_images.npy', allow_pickle=True)
unlabeled_images = np.load('/home/adeleh/MICCAI-2022/UMIS-data/medical-data/synaps/unlabeled_images.npy', allow_pickle=True)

organs = {0:"background", 1:"spleen", 2:"left_kidney", 3:"right_kidney", 6:"liver", 8:"left_muscle", 9:"right_muscle"}
selected_organ = 2
print("\nselected organ:", organs[selected_organ])

images = {}
labels = {}
for i in range(30):
    img = labeled_images[i].get('image')[30:70, :, :]
    img = resize(img, (40, 256, 256), anti_aliasing=True)
    id_ = labeled_images[i].get('id')
    images[id_] = ((img - img.min()) / (img.max() - img.min())).astype('float')
    lbl = labeled_images[i].get('label')[30:70, :, :]
    lbl = np.where(lbl == selected_organ, np.ones_like(lbl), np.zeros_like(lbl))
    lbl = resize(lbl, (40, 256, 256), anti_aliasing=False)
    lbl = np.where(lbl > 0, np.ones_like(lbl), np.zeros_like(lbl))
    labels[id_] = lbl
print("\nData loaded successfully. Total patients:", len(images))
number_of_patients = len(images)


# //////////////////////////////////// Args /////////////////////////////////////////////

class Args:
    def __init__(self):
        self.bs = 1
        self.loss = 'dice'
        self.load_model = "/home/adeleh/MICCAI-2022/armin/master-thesis/trained-models/256x256/2d/0250.pt"
        self.int_steps = 7
        self.int_downsize = 2

args = Args()


# ///////////////////////////////////// loss ////////////////////////////////////////////

class Dice:
    """
    N-D dice for segmentation
    """

    def loss(self, y_true, y_pred):
        y_true = torch.where(y_true > 0, torch.ones_like(y_true), torch.zeros_like(y_true))
        y_pred = torch.where(y_pred > 0, torch.ones_like(y_pred), torch.zeros_like(y_pred))
        intersect = torch.sum(y_pred * y_true)
        sum_ = torch.sum(y_true) + torch.sum(y_pred)
        if sum_ == 0:
            return False
        dice = (2 * intersect) / sum_
        return dice

if args.loss == 'ncc':
    sim_loss_func = vxm.losses.NCC().loss
elif args.loss == 'mse':
    sim_loss_func = vxm.losses.MSE().loss
elif args.loss == 'dice':
    sim_loss_func = Dice().loss
else:
    raise ValueError('loss should be "mse" or "ncc" or "dice", but found "%s"' % args.image_loss)

# /////////////////////////////////////// model //////////////////////////////////////////

enc_nf = [16, 32, 32, 32, 32]
dec_nf = [32, 32, 32, 32, 32, 16, 16]

if args.load_model:
    model = vxm.networks.VxmDense.load(args.load_model, device)
else:
    model = vxm.networks.VxmDense(
        inshape=(256, 256),
        nb_unet_features=[enc_nf, dec_nf],
        int_steps=args.int_steps,
        int_downsize=args.int_downsize
    )

model.to(device)
_ = model.eval()

print('number of all params:', sum(p.numel() for p in model.parameters()))
print('number of trainable params:', sum(p.numel() for p in model.parameters() if p.requires_grad))


# ///////////////////////////////////// evaluate ////////////////////////////////////////////

print("\nEvaluation started.")
patients_loss = []
evaluation_start_time = time.time()
k = 1

with torch.no_grad():
    for p_imgs, p_lbs in zip(images.values(), labels.values()):
        p_loss = 0
        p_slices = 0
        imgs = torch.tensor(p_imgs).unsqueeze(1).to(device).float()
        lbs = torch.tensor(p_lbs).unsqueeze(1).to(device).float()

        for i in range((p_imgs.shape[0] - 1) // k):
            # shape = (bs, 1, W, H)
            moving_img = imgs[i * k: (i + 1) * k]
            fixed_img = imgs[i * k + 1: (i + 1) * k + 1]

            moving_lb = lbs[i * k: (i + 1) * k]
            fixed_lb = lbs[i * k + 1: (i + 1) * k + 1]

            # predict
            moved_img, flow = model(moving_img, fixed_img, registration=True)

            moved_lb = model.transformer(moving_lb, flow)

            # calculate loss
            loss = sim_loss_func(fixed_lb, moved_lb)
            if loss == False:
                continue

            p_loss += loss * k
            p_slices += k

        patients_loss.append((p_loss / p_slices).detach().cpu())

# print evaluation info
if args.loss == 'dice':
    msg = 'dice-score= %.4f, ' % (sum(patients_loss) / len(patients_loss))
else:
    msg = 'mse= %.4e, ' % (sum(patients_loss) / len(patients_loss))
msg += 'time= %.4f ' % (time.time() - evaluation_start_time)
print(msg, flush=True)