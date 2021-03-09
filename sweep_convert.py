# Python
import numpy as np
import time
import cv2
import math

# Custom
try:
    import kittiloader.kitti as kitti
    import kittiloader.batch_loader as batch_loader
except:
    import kitti
    import batch_loader

# Data Loading Module
import torch.multiprocessing
import torch.multiprocessing as mp
from torch.multiprocessing import Process, Queue, Value, cpu_count
import utils.img_utils as img_utils
import utils.misc_utils as misc_utils
import external.utils_lib.utils_lib as kittiutils
import torch.nn.functional as F
import json
from easydict import EasyDict
import warping.view as View
import torchvision.transforms as transforms
from models.get_model import get_model
from utils.torch_utils import bias_parameters, weight_parameters, \
    load_checkpoint, save_checkpoint, AdamW

def load_datum(path, name, indx):
    datum = dict()
    # Generate Paths
    date = name.split("_drive")[0]
    index_str = "%06d" % (indx,)
    sweep_path = path + "/" + date + "/" + name + "/sweep/" + index_str + ".npy"
    left_img_path = path + "/" + date + "/" + name + "/left_img/" + index_str + ".png"
    right_img_path = path + "/" + date + "/" + name + "/right_img/" + index_str + ".png"
    nir_img_path = path + "/" + date + "/" + name + "/nir_img/" + index_str + ".png"
    velo_path = path + "/" + date + "/" + name + "/lidar/" + index_str + ".bin"
    json_path = path + "/" + date + "/" + name + "/calib.json"
    # Load Data
    datum["sweep_arr"] = np.load(sweep_path).astype(np.float32)
    datum["velodata"] = np.fromfile(velo_path, dtype=np.float32).reshape((-1, 4))
    datum["left_img"] = cv2.imread(left_img_path)
    datum["right_img"] = cv2.imread(right_img_path)
    datum["nir_img"] = cv2.imread(nir_img_path)
    datum["left_img"] = cv2.resize(datum["left_img"], None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA)
    datum["nir_img"] = cv2.resize(datum["nir_img"], None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
    datum["right_img"] = cv2.resize(datum["right_img"], None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA)
    # Load Json
    with open(json_path) as json_file:
        calib = json.load(json_file)
    datum["large_intr"] = np.array(calib["left_P"]).astype(np.float32) / 4.
    datum["large_intr"][2,2] = 1.
    datum["M_velo2left"] = np.linalg.inv(np.array(calib["left_2_lidar"]).astype(np.float32))
    datum["large_size"] = [datum["left_img"].shape[1], datum["left_img"].shape[0]]
    datum["M_left2right"] = np.array(calib["left_2_right"]).astype(np.float32)
    datum["M_right2left"] = np.linalg.inv(datum["M_left2right"])
    datum["M_left2LC"] = np.array(calib["left_2_lc"]).astype(np.float32)
    datum["M_right2LC"] = np.matmul(datum["M_right2left"], datum["M_left2LC"])
    datum["D_lc"] = np.array([-0.033918, 0.027494, -0.001691, -0.001078, 0.000000]).astype(np.float32)
    datum["K_lc"] = np.array([
        [893.074542/2, 0.000000, 524.145998/2],
        [0.000000, 893.177518/2, 646.766885/2],
        [0.000000, 0.000000, 1.000000]
    ]).astype(np.float32)
    datum["K_lc"] /= 2
    datum["K_lc"][2,2] = 1.
    datum["lc_size"] = [256, 320]
    datum["M_velo2right"] = np.matmul(datum["M_left2right"], datum["M_velo2left"])
    datum["M_velo2LC"] = np.matmul(datum["M_left2LC"], datum["M_velo2left"])
    datum["d_candi"] = img_utils.powerf(3, 18, 64, 1.)
    datum["d_candi_up"] = img_utils.powerf(3, 18, 128, 1.)

    # Easydict
    datum = EasyDict(datum)
    return datum

# Load
datum = load_datum("/media/raaj/Storage/sweep_data", "2021_03_05_drive_0004_sweep", 6)

# Undistort LC
datum.nir_img = cv2.undistort(datum.nir_img, datum.K_lc, datum.D_lc)
for i in range(0, datum.sweep_arr.shape[0]):
    datum.sweep_arr[i, :,:, 0] = cv2.undistort(datum.sweep_arr[i, :,:, 0], datum.K_lc, datum.D_lc)
    datum.sweep_arr[i, :,:, 1] = cv2.undistort(datum.sweep_arr[i, :,:, 1], datum.K_lc, datum.D_lc)

# Depths
large_params = {"filtering": 2, "upsample": 0}
datum["left_depth"] = kittiutils.generate_depth(datum.velodata, datum.large_intr, datum.M_velo2left, datum.large_size[0], datum.large_size[1], large_params)
datum["right_depth"] = kittiutils.generate_depth(datum.velodata, datum.large_intr, datum.M_velo2right, datum.large_size[0], datum.large_size[1], large_params)
datum["lc_depth"] = kittiutils.generate_depth(datum.velodata, datum.K_lc, datum.M_velo2LC, datum.lc_size[0], datum.lc_size[1], large_params)

# # Visualize Depth Check
# left_depth_debug = datum.left_img.copy().astype(np.float32)/255
# left_depth_debug[:,:,0] += datum.left_depth
# right_depth_debug = datum.right_img.copy().astype(np.float32)/255
# right_depth_debug[:,:,0] += datum.right_depth
# lc_depth_debug = datum.nir_img.copy().astype(np.float32)/255
# lc_depth_debug[:,:,0] += datum.lc_depth
# cv2.imshow("left_depth", left_depth_debug)
# cv2.imshow("right_depth", right_depth_debug)
# cv2.imshow("lc_depth", lc_depth_debug)
# cv2.waitKey(1)

# Compute
start = time.time()
datum.left_feat_int_tensor, datum.left_feat_z_tensor, datum.left_mask_tensor, datum.left_feat_mask_tensor, _ = img_utils.lcsweep_to_rgbsweep(
    sweep_arr=datum.sweep_arr, dmap_large=datum.left_depth, rgb_intr=datum.large_intr, rgb_size=datum.large_size, lc_intr=datum.K_lc, lc_size=datum.lc_size, M_left2LC=datum.M_left2LC)
datum.right_feat_int_tensor, datum.right_feat_z_tensor, datum.right_mask_tensor, datum.right_feat_mask_tensor, _ = img_utils.lcsweep_to_rgbsweep(
    sweep_arr=datum.sweep_arr, dmap_large=datum.right_depth, rgb_intr=datum.large_intr, rgb_size=datum.large_size, lc_intr=datum.K_lc, lc_size=datum.lc_size, M_left2LC=datum.M_right2LC)

# fag = datum.left_feat_mask_tensor
# fag[torch.isnan(datum.left_feat_mask_tensor)] = 1
# datum.left_mask_tensor = (torch.sum(fag, dim=0) > 0).float()

# Left
feat_int_tensor = datum.left_feat_int_tensor
feat_z_tensor = datum.left_feat_z_tensor
mask_tensor = datum.left_mask_tensor
feat_mask_tensor = datum.left_feat_mask_tensor
rgb_img = datum.left_img
depth_img = torch.tensor(datum.left_depth)
# Right
# feat_int_tensor = datum.right_feat_int_tensor
# feat_z_tensor = datum.right_feat_z_tensor
# mask_tensor = datum.right_mask_tensor
# feat_mask_tensor = datum.right_feat_mask_tensor
# rgb_img = datum.right_img
# depth_img = datum.right_depth

# Setup LC Model
class Network():
    def __init__(self, datum, mode="lc"):
        self.transforms = None
        self.index = 0
        self.prev_index = -1
        self.just_started = True
        self.mode = mode

        # Gen Model Datum
        self.param = dict()
        self.param["d_candi"] = datum["d_candi"]
        self.param["size_rgb"] = datum["large_size"]
        #intrinsics = torch.tensor(datum["large_intr"])[0:3,0:3]
        intrinsics_up = torch.tensor(datum["large_intr"][0:3,0:3]).unsqueeze(0)
        intrinsics = intrinsics_up / 4; intrinsics[0,2,2] = 1.
        s_width = datum["large_size"][0]/4
        s_height = datum["large_size"][1]/4
        focal_length = np.mean([intrinsics_up[0,0,0], intrinsics_up[0,1,1]])
        h_fov = math.degrees(math.atan(intrinsics_up[0,0, 2] / intrinsics_up[0,0, 0]) * 2)
        v_fov = math.degrees(math.atan(intrinsics_up[0,1, 2] / intrinsics_up[0,1, 1]) * 2)
        pixel_to_ray_array = View.normalised_pixel_to_ray_array(\
                width= int(s_width), height= int(s_height), hfov = h_fov, vfov = v_fov,
                normalize_z = True)
        pixel_to_ray_array_2dM = np.reshape(np.transpose( pixel_to_ray_array, axes= [2,0,1] ), [3, -1])
        pixel_to_ray_array_2dM = torch.from_numpy(pixel_to_ray_array_2dM.astype(np.float32)).unsqueeze(0)
        left_2_right = torch.tensor(datum["M_left2right"])
        if self.mode == "stereo" or self.mode == "stereo_lc":
            src_cam_poses = torch.cat([left_2_right.unsqueeze(0), torch.eye(4).unsqueeze(0)]).unsqueeze(0)
        elif self.mode == "mono" or self.mode == "mono_lc":
            src_cam_poses = torch.cat([torch.eye(4).unsqueeze(0), torch.eye(4).unsqueeze(0)]).unsqueeze(0)
        else:
            src_cam_poses = torch.cat([torch.eye(4).unsqueeze(0), torch.eye(4).unsqueeze(0)]).unsqueeze(0)
        self.model_datum = dict()
        self.model_datum["intrinsics"] = intrinsics.cuda()
        self.model_datum["intrinsics_up"] = intrinsics_up.cuda()
        self.model_datum["unit_ray"] = pixel_to_ray_array_2dM.cuda()
        self.model_datum["src_cam_poses"] = src_cam_poses.cuda()
        self.model_datum["d_candi"] = self.param["d_candi"]
        self.model_datum["d_candi_up"] = self.param["d_candi"]
        self.model_datum["rgb"] = None
        self.model_datum["prev_output"] = None
        self.model_datum["prev_lc"] = None
        self.rgb_pinned = torch.zeros((1,2,3,self.param["size_rgb"][1], self.param["size_rgb"][0])).float().pin_memory()
        self.dpv_pinned = torch.zeros((1,64,int(self.param["size_rgb"][1]), int(self.param["size_rgb"][0]))).float().pin_memory()
        self.pred_depth_pinned = torch.zeros((int(self.param["size_rgb"][1]), int(self.param["size_rgb"][0]))).float().pin_memory()
        self.true_depth_pinned = torch.zeros((int(self.param["size_rgb"][1]), int(self.param["size_rgb"][0]))).float().pin_memory()
        self.unc_pinned = torch.zeros(1,64, int(self.param["size_rgb"][0])).float().pin_memory()
        __imagenet_stats = {'mean': [0.485, 0.456, 0.406],\
                            'std': [0.229, 0.224, 0.225]}
        self.transformer = transforms.Normalize(**__imagenet_stats)

        # Load Model
        if self.mode == "stereo":
            model_name = 'default_stereo_ilim'
        elif self.mode == "mono":
            model_name = 'default_ilim'
        elif self.mode == "mono_lc":
            model_name = 'default_exp7_lc_ilim'
        elif self.mode == 'stereo_lc':
            model_name = 'default_stereo_exp7_lc_ilim'
        elif self.mode == 'lc':
            model_name = 'default_sweep'
        elif self.mode == 'mono_318':
            model_name = 'default_exp7_lc_ilim_318'
        cfg_path = 'configs/' + model_name + '.json'
        model_path = ''
        with open(cfg_path) as f:
            self.cfg = EasyDict(json.load(f))
        self.model = get_model(self.cfg, 0)
        epoch, weights = load_checkpoint('outputs/checkpoints/' + model_name + '/' + model_name + '_model_best.pth.tar')
        from collections import OrderedDict
        new_weights = OrderedDict()
        model_keys = list(self.model.state_dict().keys())
        weight_keys = list(weights.keys())
        for a, b in zip(model_keys, weight_keys):
            new_weights[a] = weights[b]
        weights = new_weights
        self.model.load_state_dict(weights)
        self.model = self.model.cuda()
        self.model.eval()
        print("Model Loaded")

    def set_lc_params(self, img1):
        image = (img1.astype(np.float32))/255.
        inp1 = torch.tensor(image).permute(2,0,1)
        inp1 = inp1[[2,1,0], :, :]
        inp1 = self.transformer(inp1)
        inp2 = torch.tensor(image).permute(2,0,1)
        inp2 = inp2[[2,1,0], :, :]
        inp2 = self.transformer(inp2)
        self.rgb_pinned[:] = torch.cat([inp1.unsqueeze(0), inp2.unsqueeze(0)]).unsqueeze(0)
        self.model_datum["rgb"] = self.rgb_pinned.cuda(non_blocking=False)

    def run_lc_network(self):
        self.model.eval()
        output = self.model([self.model_datum])[0]
        output_refined = output["output_refined"][-1]
        return output_refined

# RGB Network
depth_network = Network(datum, mode='mono_318')
depth_network.set_lc_params(datum['left_img'])
dpv_output = depth_network.run_lc_network()
depth_output = img_utils.dpv_to_depthmap(dpv_output, depth_network.model_datum["d_candi"], BV_log=True)
unc_field_predicted, debugmap = img_utils.gen_ufield(dpv_output, depth_network.model_datum["d_candi"], depth_network.model_datum["intrinsics_up"].squeeze(0), BV_log=True, 
                                cfgx={"unc_ang": 0, "unc_shift": 1, "unc_span": 0.3})
dpv_truth = img_utils.gen_dpv_withmask(torch.tensor(datum["left_depth"]).unsqueeze(0), (torch.tensor(datum["left_depth"]) > 0).float().unsqueeze(0).unsqueeze(0), depth_network.model_datum["d_candi"])
unc_field_truth, _ = img_utils.gen_ufield(dpv_truth, depth_network.model_datum["d_candi"], depth_network.model_datum["intrinsics_up"].squeeze(0), BV_log=False, 
                                cfgx={"unc_ang": 0, "unc_shift": 1, "unc_span": 0.3})

# Visualize Top Down
field_visual = np.zeros((unc_field_truth.shape[1], unc_field_truth.shape[2], 3))
field_visual[:,:,0] = unc_field_predicted[0,:,:].detach().cpu().numpy()*3
field_visual[:,:,1] = unc_field_predicted[0,:,:].detach().cpu().numpy()*3
field_visual[:,:,2] = unc_field_truth[0,:,:].detach().cpu().numpy()*3
field_visual = cv2.resize(field_visual, None, fx=1, fy=2, interpolation = cv2.INTER_CUBIC)
rgb_debug = datum["left_img"].copy().astype(np.float32)/255
rgb_debug[:,:,0] += debugmap[0,:,:].detach().cpu().numpy()
cv2.imshow("field_visual", field_visual)
cv2.imshow("depth_output", depth_output.squeeze(0).detach().cpu().numpy()/100)
cv2.imshow("rgb_debug", rgb_debug)
cv2.waitKey(0)
"""
Train a default model that strictly does 318 disable other losses and only uses one image 
Transfer learn to ILIM that mixes our data too
"""
LOOK AT TODO

# LC Network
lc_network = Network(datum, mode='lc')
lc_network.set_lc_params(datum["left_img"])
lc_output = lc_network.run_lc_network().detach().cpu().squeeze(0)

# LC Model Test
d_candi = datum["d_candi_up"]
peak_gt = torch.max(feat_int_tensor, dim=0)[0] / 255
peak_pred = lc_output[0,:,:]
sigma_pred = lc_output[1,:,:]
inten_sigma = torch.ones(peak_gt.shape)
mean_scaling = peak_gt
mean_intensities, _ = img_utils.lc_intensities_to_dist(d_candi=feat_z_tensor.permute(1,2,0), placement=depth_img.unsqueeze(-1), 
intensity=0, inten_sigma=inten_sigma.unsqueeze(-1), noise_sigma=0.1, mean_scaling=mean_scaling.unsqueeze(-1))
mean_intensities = mean_intensities.permute(2,0,1) # 128, 256, 320

# # Viz Peak?
# print(torch.sum(peak_gt))
# print(torch.sum(peak_pred))
cv2.imshow("peak_gt", peak_gt.cpu().numpy())
cv2.imshow("peak_pred", peak_pred.cpu().numpy())
#cv2.imshow("sigma_pred", sigma_pred.cpu().numpy())
cv2.waitKey(0)

# Plotting
import random
from matplotlib import pyplot as plt
def mouse_callback(event,x,y,flags,param):
    global mouseX,mouseY, combined_image, sweep_arr, dmap_large, feat_int_tensor, feat_mask_tensor, mean_intensities, d_candi
    if event == cv2.EVENT_LBUTTONDBLCLK:
        mouseX,mouseY = x,y
        print(mouseX,mouseY)
        rgb = (random.random(), random.random(), random.random())

        # Extract vals
        disp_z = feat_z_tensor[:,y,x]
        disp_i = feat_int_tensor[:,y,x]/255.
        first_nan = np.isnan(disp_z).argmax(axis=0)
        if first_nan:
            disp_z = disp_z[0:first_nan]
            disp_i = disp_i[0:first_nan]

        plt.figure(0)
        plt.plot(disp_z, disp_i, c=rgb, marker='*')
        #plt.figure(1)
        plt.plot(d_candi, mean_intensities[:,y,x], c=rgb, marker='*')

        plt.pause(0.1)

cv2.namedWindow('rgbimg')
cv2.setMouseCallback('rgbimg',mouse_callback)

while 1:
    cv2.imshow("rgbimg", rgb_img/255. + mask_tensor.squeeze(0).unsqueeze(-1).numpy())
    cv2.waitKey(15)


# Gather Operation
# feat_z_tensor_temp = feat_z_tensor.clone()
# feat_z_tensor_temp[torch.isnan(feat_z_tensor)] = 1000
# inds = torch.argmin(torch.abs(dmap_large.reshape(1, 256, 320) - feat_z_tensor_temp), dim=0)  # (320, 240)
# result = torch.gather(feat_int_tensor, 0, inds.unsqueeze(0))
#result = torch.gather(intensity_values, 0, inds.unsqueeze(0))

# # Reduce Tensor Size here itself?
# print(mask_tensor.shape)
# feat_int_tensor = F.interpolate(feat_int_tensor.unsqueeze(0), size=[64, 80], mode='nearest').squeeze(0)
# feat_z_tensor = F.interpolate(feat_z_tensor.unsqueeze(0), size=[64, 80], mode='nearest').squeeze(0)
# mask_tensor = F.interpolate(mask_tensor.unsqueeze(0), size=[64, 80], mode='nearest').squeeze(0)
# rgbimg = cv2.resize(rgbimg, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA)