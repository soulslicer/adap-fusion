import torch
import torch.nn as nn
import torch.nn.functional as F
import math
import numpy as np
import warping.homography as warp_homo
import utils.img_utils as img_utils
import random

def conv(in_planes, out_planes, kernel_size=3, stride=1, dilation=0, padding=1, isReLU=True):
    if isReLU:
        return nn.Sequential(
            nn.Conv2d(in_planes, out_planes, kernel_size=kernel_size, stride=stride,
                      padding=padding, bias=True),
            nn.LeakyReLU(0.1, inplace=True)
        )
    else:
        return nn.Sequential(
            nn.Conv2d(in_planes, out_planes, kernel_size=kernel_size, stride=stride,
                      dilation=dilation,
                      padding=padding, bias=True),
            nn.LeakyReLU(0.1, inplace=True)
        )

def convbn(in_planes, out_planes, kernel_size, stride, pad, dilation, bn_running_avg=False):
    return nn.Sequential(nn.Conv2d(in_planes, out_planes,
                                   kernel_size=kernel_size, stride=stride,
                                   padding=dilation if dilation > 1 else pad,
                                   dilation=dilation, bias=False),
                         nn.BatchNorm2d(out_planes, track_running_stats=bn_running_avg))

def convbn_3d(in_planes, out_planes, kernel_size, stride, pad, bn_running_avg=False):
    return nn.Sequential(nn.Conv3d(in_planes, out_planes,
                                   kernel_size=kernel_size, padding=pad,
                                   stride=stride,bias=False),
                         nn.BatchNorm3d(out_planes, track_running_stats=bn_running_avg))

def conv2d_leakyRelu(ch_in, ch_out, kernel_size, stride, pad, use_bias=True, dilation = 1):
    r'''
    Conv2d + leakyRelu
    '''
    return nn.Sequential(
            nn.Conv2d(
                ch_in, ch_out, kernel_size=kernel_size, stride = stride,
                padding = dilation if dilation >1 else pad, dilation = dilation, bias= use_bias),
            nn.LeakyReLU())

def conv2dTranspose_leakyRelu(ch_in, ch_out, kernel_size, stride, pad, use_bias = True, dilation=1 ):
    r'''
    ConvTrans2d + leakyRelu
    '''
    return nn.Sequential(
            nn.ConvTranspose2d(ch_in, ch_out, kernel_size = kernel_size, stride =stride,
                padding= pad, bias = use_bias, dilation = dilation),
            nn.LeakyReLU())

class ResConvBlock(torch.nn.Module):
    def __init__(self, In_D, Out_D, BN=False):
        """
        In the constructor we instantiate two nn.Linear modules and assign them as
        member variables.
        """
        super(ResConvBlock, self).__init__()

        if BN:
            self.a = nn.Sequential(
                convbn(In_D, Out_D, 3, 1, 1, 1, False), nn.ReLU(inplace=True)
            )
            self.b = nn.Sequential(
                convbn(Out_D, Out_D, 3, 1, 1, 1, False), nn.ReLU(inplace=True)
            )
            self.c = nn.Sequential(
                convbn(Out_D, Out_D, 3, 1, 1, 1, False), nn.ReLU(inplace=True)
            )
        else:
            self.a = nn.Sequential(
                nn.Conv2d(In_D, Out_D, 3, 1, 1),
                nn.PReLU(Out_D)
            )
            self.b = nn.Sequential(
                nn.Conv2d(Out_D, Out_D, 3, 1, 1),
                nn.PReLU(Out_D)
            )
            self.c = nn.Sequential(
                nn.Conv2d(Out_D, Out_D, 3, 1, 1),
                nn.PReLU(Out_D)
            )

    def forward(self, x):
        a_output = self.a(x)
        b_output = self.b(a_output)
        c_output = self.c(b_output)
        output = torch.cat([a_output, b_output, c_output], 1)
        return output

class ABlock3x3(torch.nn.Module):
    def __init__(self, In_D, Out_D, Depth=64, SubDepth=256, C=7, BN=False):
        """
        In the constructor we instantiate two nn.Linear modules and assign them as
        member variables.
        """
        super(ABlock3x3, self).__init__()

        modules = [ResConvBlock(In_D, Depth, BN)]
        for i in range(0, C):
            modules.append(ResConvBlock(Depth * 3, Depth, BN))
        modules.append(nn.Conv2d(Depth * 3, SubDepth, 1, 1, 0))
        modules.append(nn.PReLU(SubDepth))
        modules.append(nn.Conv2d(SubDepth, Out_D, 1, 1, 0))

        self.net = nn.Sequential(
            *modules
        )
        self.apply(self.weight_init)

    def weight_init(self, m):
        if isinstance(m, nn.Conv2d):
            n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
            m.weight.data.normal_(0, math.sqrt(2. / n))
        elif isinstance(m, nn.BatchNorm2d):
            m.weight.data.fill_(1)
            m.bias.data.zero_()
        elif isinstance(m, nn.Linear):
            m.bias.data.zero_()

    def forward(self, x):
        return self.net(x)

class BasicBlock(nn.Module):
    expansion = 1

    def __init__(self, inplanes, planes, stride, downsample, pad, dilation, bn_running_avg=False):
        super(BasicBlock, self).__init__()
        self.conv1 = nn.Sequential(convbn(inplanes, planes, 3, stride, pad, dilation, bn_running_avg),
                                   nn.ReLU(inplace=True))
        self.conv2 = convbn(planes, planes, 3, 1, pad, dilation, bn_running_avg)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        out = self.conv1(x)
        out = self.conv2(out)

        if self.downsample is not None:
            x = self.downsample(x)
        out += x

        return out

class BaseEncoder(nn.Module):
    def __init__(self, feature_dim=32, bn_running_avg=False, multi_scale=True):
        '''
        inputs:
        multi_scale - if output multi-sclae features:
        [1/4 scale of input image, 1/2 scale of input image]
        '''

        super(BaseEncoder, self).__init__()

        MUL = feature_dim / 64.
        S0 = int(16 * MUL)
        S1 = int(32 * MUL)
        S2 = int(64 * MUL)
        S3 = int(128 * MUL)

        self.inplanes = S1
        self.multi_scale = multi_scale

        self.bn_ravg = bn_running_avg
        self.firstconv = nn.Sequential(convbn(3, S1, 3, 2, 1, 1, self.bn_ravg), nn.ReLU(inplace=True),
                                       convbn(S1, S1, 3, 1, 1, 1, self.bn_ravg), nn.ReLU(inplace=True),
                                       convbn(S1, S1, 3, 1, 1, 1, self.bn_ravg), nn.ReLU(inplace=True))

        self.layer1 = self._make_layer(BasicBlock, S1, 3, 1, 1, 1)
        self.layer2 = self._make_layer(BasicBlock, S2, S0, 2, 1, 1)

        # Chao: this is different from the dilation in the paper (2)
        self.layer3 = self._make_layer(BasicBlock, S3, 3, 1, 1, 1)

        # Chao: in the paper, this is 4
        self.layer4 = self._make_layer(BasicBlock, S3, 3, 1, 1, 2)

        self.branch1 = nn.Sequential(nn.AvgPool2d((64, 64), stride=(64, 64)),
                                     convbn(S3, S1, 1, 1, 0, 1, self.bn_ravg),
                                     nn.ReLU(inplace=True))

        self.branch2 = nn.Sequential(nn.AvgPool2d((32, 32), stride=(32, 32)),
                                     convbn(S3, S1, 1, 1, 0, 1, self.bn_ravg),
                                     nn.ReLU(inplace=True))

        self.branch3 = nn.Sequential(nn.AvgPool2d((16, 16), stride=(16, 16)),
                                     convbn(S3, S1, 1, 1, 0, 1, self.bn_ravg),
                                     nn.ReLU(inplace=True))

        self.branch4 = nn.Sequential(nn.AvgPool2d((8, 8), stride=(8, 8)),
                                     convbn(S3, S1, 1, 1, 0, 1, self.bn_ravg),
                                     nn.ReLU(inplace=True))

        self.lastconv = nn.Sequential(convbn(S1 * 4 + S2 + S3, S3, 3, 1, 1, 1, self.bn_ravg),
                                      nn.ReLU(inplace=True),
                                      nn.Conv2d(S3,
                                                feature_dim, kernel_size=1, padding=0, stride=1, bias=False))

        self.apply(self.weight_init)

    def _make_layer(self, block, planes, blocks, stride, pad, dilation):
        downsample = None

        if stride != 1 or self.inplanes != planes * block.expansion:
            downsample = nn.Sequential(
                nn.Conv2d(self.inplanes, planes * block.expansion,
                          kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm2d(planes * block.expansion, track_running_stats=self.bn_ravg), )

        layers = []
        layers.append(block(self.inplanes, planes, stride, downsample, pad, dilation, self.bn_ravg))
        self.inplanes = planes * block.expansion
        for i in range(1, blocks):
            layers.append(block(self.inplanes, planes, 1, None, pad, dilation, self.bn_ravg))

        return nn.Sequential(*layers)

    def weight_init(self, m):
        if isinstance(m, nn.Conv2d):
            n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
            m.weight.data.normal_(0, math.sqrt(2. / n))
        elif isinstance(m, nn.Conv3d):
            n = m.kernel_size[0] * m.kernel_size[1] * m.kernel_size[2] * m.out_channels
            m.weight.data.normal_(0, math.sqrt(2. / n))
        elif isinstance(m, nn.BatchNorm2d):
            m.weight.data.fill_(1)
            m.bias.data.zero_()
        elif isinstance(m, nn.BatchNorm3d):
            m.weight.data.fill_(1)
            m.bias.data.zero_()
        elif isinstance(m, nn.Linear):
            m.bias.data.zero_()

    def forward(self, x):
        output = self.firstconv(x) # [2, 32, 128, 192]
        output_layer1 = self.layer1(output) # [2, 32, 128, 192]
        output_raw = self.layer2(output_layer1) # [2, 64, 64, 96]
        output = self.layer3(output_raw) # [2, 128, 64, 96]
        output_skip = self.layer4(output) # [2, 128, 64, 96]

        output_branch1 = self.branch1(output_skip)
        output_branch1 = F.upsample(output_branch1,
                                    (output_skip.size()[2], output_skip.size()[3]), mode='bilinear', align_corners=True)

        output_branch2 = self.branch2(output_skip)
        output_branch2 = F.upsample(output_branch2, (output_skip.size()[2], output_skip.size()[3]), mode='bilinear',
                                    align_corners=True)

        output_branch3 = self.branch3(output_skip)
        output_branch3 = F.upsample(output_branch3, (output_skip.size()[2], output_skip.size()[3]), mode='bilinear',
                                    align_corners=True)

        output_branch4 = self.branch4(output_skip)
        output_branch4 = F.upsample(output_branch4, (output_skip.size()[2], output_skip.size()[3]), mode='bilinear',
                                    align_corners=True)

        output_feature = torch.cat(
            (output_raw, output_skip, output_branch4, output_branch3, output_branch2, output_branch1), 1)
        output_feature = self.lastconv(output_feature)

        if self.multi_scale:
            return output_layer1, output_raw, output_feature
        else:
            return output_feature

class BaseDecoder(nn.Module):
    '''
    The refinement taking the DPV, using the D dimension as the feature dimension, plus the image features,
    then upsample the DPV (4 time the input dpv resolution)
    '''

    def __init__(self, C0, C1, C2, D=64, upsample_D=False):
        '''
        Inputs:

        C0 - feature channels in .25 image resolution feature,
        C1 - feature cnahnels in .5 image resolution feature,
        C2 - feature cnahnels in 1 image resolution feature,

        D - the length of d_candi, we will treat the D dimension as the feature dimension
        upsample_D - if upsample in the D dimension
        '''
        super(BaseDecoder, self).__init__()
        in_channels = D + C0

        if upsample_D:
            D0 = 2 * D
            D1 = 2 * D0
        else:
            D0 = D
            D1 = D

        self.conv0 = conv2d_leakyRelu(
            ch_in=in_channels, ch_out=in_channels, kernel_size=3, stride=1, pad=1, use_bias=True)

        self.conv0_1 = conv2d_leakyRelu(
            ch_in=in_channels, ch_out=in_channels, kernel_size=3, stride=1, pad=1, use_bias=True)

        self.trans_conv0 = conv2dTranspose_leakyRelu(
            ch_in=in_channels, ch_out=D0, kernel_size=4, stride=2, pad=1, use_bias=True)

        self.conv1 = conv2d_leakyRelu(
            ch_in=D0 + C1, ch_out=D0 + C1, kernel_size=3, stride=1, pad=1, use_bias=True)

        self.conv1_1 = conv2d_leakyRelu(
            ch_in=D0 + C1, ch_out=D0 + C1, kernel_size=3, stride=1, pad=1, use_bias=True)

        self.trans_conv1 = conv2dTranspose_leakyRelu(
            ch_in=D0 + C1, ch_out=D1, kernel_size=4, stride=2, pad=1, use_bias=True)

        self.conv2 = conv2d_leakyRelu(
            ch_in=D1 + C2, ch_out=D1 + C2, kernel_size=3, stride=1, pad=1, use_bias=True)

        self.conv2_1 = conv2d_leakyRelu(
            ch_in=D1 + C2, ch_out=D1, kernel_size=3, stride=1, pad=1, use_bias=True)

        self.conv2_2 = nn.Conv2d(D1, D1, kernel_size=3, stride=1, padding=1, bias=True)

        self.apply(self.weight_init)

    def forward(self, dpv_raw, img_features):
        '''
        dpv_raw - the low resolution (.25 image size) dpv (N D H W)
        img_features - list of image features [ .25 image size, .5 image size, 1 image size]

        NOTE:
        dpv_raw from 0, 1 (need to exp() if in log scale)

        output dpv in log-scale
        '''

        conv0_out = self.conv0(torch.cat([dpv_raw, img_features[0]], dim=1))
        conv0_1_out = self.conv0_1(conv0_out)

        trans_conv0_out = self.trans_conv0(conv0_1_out)

        conv1_out = self.conv1(torch.cat([trans_conv0_out, img_features[1]], dim=1))
        conv1_1_out = self.conv1_1(conv1_out)

        trans_conv1_out = self.trans_conv1(conv1_1_out)
        conv2_out = self.conv2(torch.cat([trans_conv1_out, img_features[2]], dim=1))
        conv2_1_out = self.conv2_1(conv2_out)
        conv2_2_out = self.conv2_2(conv2_1_out)

        # normalization, assuming input dpv is in the log scale
        dpv_refined = F.log_softmax(conv2_2_out, dim=1)

        return dpv_refined

    def weight_init(self, m):
        if isinstance(m, nn.Conv2d):
            n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
            m.weight.data.normal_(0, math.sqrt(2. / n))
        elif isinstance(m, nn.BatchNorm2d):
            m.weight.data.fill_(1)
            m.bias.data.zero_()
        elif isinstance(m, nn.Linear):
            m.bias.data.zero_()
        elif isinstance(m, nn.ConvTranspose2d):
            n = m.kernel_size[1]
            factor = (n + 1) // 2
            if n % 2 == 1:
                center = factor - 1
            else:
                center = factor - .5

            og = np.ogrid[:n, :n]
            weights_np = (1 - abs(og[0] - center) / factor) * (1 - abs(og[1] - center) / factor)
            m.weight.data.copy_(torch.from_numpy(weights_np))


class Base3D(nn.Module):
    def __init__(self, input_volume_channels, feature_dim=32, dres_count=4, bn_running_avg=False, id=0):
        '''
        inputs:
        input_volume_channels - the # of channels for the input volume
        '''
        super(Base3D, self).__init__()
        self.in_channels = input_volume_channels
        self.dres_count = dres_count
        self.bn_avg = bn_running_avg
        self.id = id

        # The basic 3D-CNN in PSM-net #
        self.dres0 = nn.Sequential(convbn_3d(input_volume_channels, feature_dim, 3, 1, 1, self.bn_avg),
                                   nn.ReLU(),
                                   convbn_3d(feature_dim, feature_dim, 3, 1, 1, self.bn_avg),
                                   nn.ReLU())

        self.dres_modules = []
        for i in range(0, self.dres_count):
            dres = nn.Sequential(convbn_3d(feature_dim, feature_dim, 3, 1, 1, self.bn_avg),
                                 nn.ReLU(),
                                 convbn_3d(feature_dim, feature_dim, 3, 1, 1, self.bn_avg))
            self.dres_modules.append(dres.cuda(self.id))

        self.classify = nn.Sequential(convbn_3d(feature_dim, feature_dim, 3, 1, 1, self.bn_avg),
                                      nn.ReLU(),
                                      nn.Conv3d(feature_dim, 1, kernel_size=3, padding=1, stride=1, bias=False))

        self.apply(self.weight_init)

    def weight_init(self, m):
        if isinstance(m, nn.Conv2d):
            n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
            m.weight.data.normal_(0, math.sqrt(2. / n))
        elif isinstance(m, nn.Conv3d):
            n = m.kernel_size[0] * m.kernel_size[1] * m.kernel_size[2] * m.out_channels
            m.weight.data.normal_(0, math.sqrt(2. / n))
        elif isinstance(m, nn.BatchNorm2d):
            m.weight.data.fill_(1)
            m.bias.data.zero_()
        elif isinstance(m, nn.BatchNorm3d):
            m.weight.data.fill_(1)
            m.bias.data.zero_()
        elif isinstance(m, nn.Linear):
            m.bias.data.zero_()

    def forward(self, input_volume, prob=True):
        input_volume = input_volume.contiguous()

        # cost: the intermidiate results #
        cost0 = self.dres0(input_volume)
        curr_cost = cost0
        for dres in self.dres_modules:
            curr_cost = dres(curr_cost) + curr_cost
        res_volume = self.classify(curr_cost)

        if prob:
            res_prob = F.log_softmax(res_volume, dim=2).squeeze(1)
        else:
            res_prob = res_volume.squeeze(1)

        return res_prob

class BaseModel(nn.Module):
    def __init__(self, cfg, id):
        super(BaseModel, self).__init__()
        self.cfg = cfg
        self.sigma_soft_max = self.cfg.var.sigma_soft_max
        self.feature_dim = self.cfg.var.feature_dim
        self.nmode = self.cfg.var.nmode
        self.D = self.cfg.var.ndepth
        self.bn_avg = self.cfg.var.bn_avg
        self.id = id
        self.diffhomo = True
        try: self.diffhomo = self.cfg.var.diffhomo
        except: pass
        
        # Encoder
        self.base_encoder = BaseEncoder(feature_dim = self.feature_dim, multi_scale = True, bn_running_avg = self.bn_avg)
        self.base_decoder = BaseDecoder(int(self.feature_dim), int(self.feature_dim/2), 3, D = self.D)

        # Additional
        self.conv0 = conv2d_leakyRelu(
            ch_in=self.D, ch_out=self.D, kernel_size=3, stride=1, pad=1, use_bias=True)
        self.conv0_1 = conv2d_leakyRelu(
            ch_in=self.D, ch_out=self.D, kernel_size=3, stride=1, pad=1, use_bias=True)
        self.conv0_2 = nn.Conv2d(self.D, self.D, kernel_size=3, stride=1, padding=1, bias=True)

        # Other
        if self.nmode == "exp3" or self.nmode == "exp4":
            self.based_3d = Base3D(3, dres_count=2, feature_dim=32, bn_running_avg = self.bn_avg, id = self.id)
        if self.nmode == "exp6" or self.nmode == "exp7":
            self.based_3d = Base3D(4, dres_count=2, feature_dim=32, bn_running_avg=self.bn_avg, id=self.id)
        if self.nmode == "exp7_lc":
            self.based_3d = Base3D(5, dres_count=2, feature_dim=32, bn_running_avg=self.bn_avg, id=self.id)
        if self.nmode == "lc":
            self.based_3d = Base3D(4, dres_count=2, feature_dim=32, bn_running_avg=self.bn_avg, id=self.id)
        if self.nmode == "default_df3":
            self.base_decoder2 = BaseDecoder(int(self.feature_dim), int(self.feature_dim/2), 3, D = self.D)
        if self.nmode == "exp8":
            self.base_decoder2 = BaseDecoder(int(self.feature_dim), int(self.feature_dim/2), 3, D = self.D)

        # Apply Weights
        self.apply(self.weight_init)

        # Viz
        self.viz = None

        # LC Init
        self.algo_lc = None
        self.real_lc = None
        if self.cfg.lc.enabled:
            from lc import light_curtain
            self.algo_lc = light_curtain.LightCurtain()
            # Special Case
            if "sweep" in self.cfg.data.dataset_split:
                self.real_lc = light_curtain.LightCurtain()
            print("Setup LC")

    def set_viz(self, viz):
        self.viz = viz

    def freeze_weights(self, name):
        print("Freezing: " + name)
        for param in getattr(self, name).parameters():
            param.requires_grad = False

    def weight_init(self, m):
        if isinstance(m, nn.Conv2d):
            n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
            m.weight.data.normal_(0, math.sqrt(2. / n))
        elif isinstance(m, nn.BatchNorm2d):
            m.weight.data.fill_(1)
            m.bias.data.zero_()
        elif isinstance(m, nn.Linear):
            m.bias.data.zero_()
        elif isinstance(m, nn.ConvTranspose2d):
            n = m.kernel_size[1]
            factor = (n+1) // 2
            if n %2 ==1:
                center = factor - 1
            else:
                center = factor -.5

            og = np.ogrid[:n, :n]
            weights_np = (1 - abs(og[0] - center) / factor) * (1 - abs(og[1] - center) / factor)
            m.weight.data.copy_(torch.from_numpy(weights_np))

    def init_weights(self):
        self.apply(self.weight_init)

    def forward_encoder(self, model_input):
        # "intrinsics": intrinsics, # [B, 3, 3]
        # "unit_ray": unit_ray, [B, 3, 6144]
        # "src_cam_poses": src_cam_poses, [B, 2, 4, 4]
        # "rgb": rgb [4, 2, 3,256,384]
        bsize = model_input["rgb"].shape[0]
        d_candi = model_input["d_candi"]

        # Feature Extraction
        rgb = model_input["rgb"]
        rgb_reshaped = rgb.view(rgb.shape[0]*rgb.shape[1], rgb.shape[2], rgb.shape[3], rgb.shape[4])
        feat_imgs_layer_1, feat_raw, feat_imgs = self.base_encoder(rgb_reshaped) # [8,32,128,192] [8,64,64,96]

        # Append image
        dw_rate = int(rgb_reshaped.shape[3] / feat_imgs.shape[3])
        img_features = F.avg_pool2d(rgb_reshaped, dw_rate) # [8,3,64,96]
        feat_imgs_all = torch.cat( (feat_imgs, img_features), dim=1 ) # [8,67,64,96]
        feat_imgs_layer_1 = feat_imgs_layer_1.view(rgb.shape[0], rgb.shape[1], feat_imgs_layer_1.shape[1], feat_imgs_layer_1.shape[2], feat_imgs_layer_1.shape[3])
        feat_imgs_all = feat_imgs_all.view(rgb.shape[0], rgb.shape[1], feat_imgs_all.shape[1], feat_imgs_all.shape[2], feat_imgs_all.shape[3])
        # [4,2, 32,128,192]
        # [4,2, 67,64,96]

        # Warp Cost Volume for each video batch
        if self.diffhomo:
            cost_volumes = []
            for i in range(0, bsize):

                Rs_src = model_input["src_cam_poses"][i,:-1, :3,:3]
                ts_src = model_input["src_cam_poses"][i,:-1, :3,3]

                # [1,67,64,96]
                feat_img_ref = feat_imgs_all[i,-1,:,:,:].unsqueeze(0)
                feat_imgs_src = feat_imgs_all[i,:-1,:,:,:].unsqueeze(0)

                cam_intrinsics = {"intrinsic_M_cuda": model_input["intrinsics"][i,:,:],
                                "intrinsic_M": model_input["intrinsics"][i,:,:].cpu().numpy(),
                                "unit_ray_array_2D": model_input["unit_ray"][i,:,:]}

                costV = warp_homo.est_swp_volume_v4( \
                        feat_img_ref,
                        feat_imgs_src,
                        d_candi, Rs_src, ts_src,
                        cam_intrinsics,
                        self.sigma_soft_max,
                        feat_dist = 'L2')
                # [1,128,64,96]

                cost_volumes.append(costV)

            cost_volumes = torch.cat(cost_volumes, dim=0) # [4 128 64 96]
        
        # No Warp
        else:
            cost_volumes = feat_imgs_all[:,-1,:-3,:,:]

        # Refinement (3D Conv here or not)
        costv_out0 = self.conv0( cost_volumes )
        costv_out1 = self.conv0_1( costv_out0)
        costv_out2 = self.conv0_2( costv_out1)

        # Ensure log like
        BV = F.log_softmax(costv_out2, dim=1)

        # Return BV and primary image features (in the future return others too for flow?)
        last_features = [feat_imgs_all[:,-1,:-3, :,:], feat_imgs_layer_1[:,-1,:,:,:]]
        first_features = [feat_imgs_all[:,0,:-3, :,:], feat_imgs_layer_1[:,0,:,:,:]]
        return BV, cost_volumes, last_features, first_features

    def forward_exp(self, model_input):
        bsize = model_input["rgb"].shape[0]
        d_candi = model_input["d_candi"]

        # Feature Extraction
        rgb = model_input["rgb"]
        rgb_reshaped = rgb.view(rgb.shape[0]*rgb.shape[1], rgb.shape[2], rgb.shape[3], rgb.shape[4])
        feat_imgs_layer_1, feat_raw, feat_imgs = self.base_encoder(rgb_reshaped) # [8,32,128,192] [8,64,64,96]

        # Feat Imgs
        dw_rate = int(rgb_reshaped.shape[3] / feat_imgs.shape[3])
        img_features = F.avg_pool2d(rgb_reshaped, dw_rate) # [8,3,64,96]
        feat_imgs_all = torch.cat( (feat_imgs, img_features), dim=1 ) # [8,67,64,96]
        feat_imgs_layer_1 = feat_imgs_layer_1.view(rgb.shape[0], rgb.shape[1], feat_imgs_layer_1.shape[1], feat_imgs_layer_1.shape[2], feat_imgs_layer_1.shape[3])
        feat_imgs_all = feat_imgs_all.view(rgb.shape[0], rgb.shape[1], feat_imgs_all.shape[1], feat_imgs_all.shape[2], feat_imgs_all.shape[3])
        feat_raw_all = feat_raw.view(rgb.shape[0], rgb.shape[1], feat_raw.shape[1], feat_raw.shape[2], feat_raw.shape[3])

        # Warp Cost Volume for each video batch
        if self.diffhomo:
            cost_volumes = []
            for i in range(0, bsize):

                Rs_src = model_input["src_cam_poses"][i,:-1, :3,:3]
                ts_src = model_input["src_cam_poses"][i,:-1, :3,3]

                # [1,67,64,96]
                feat_img_ref = feat_imgs_all[i,-1,:,:,:].unsqueeze(0)
                feat_imgs_src = feat_imgs_all[i,:-1,:,:,:].unsqueeze(0)

                cam_intrinsics = {"intrinsic_M_cuda": model_input["intrinsics"][i,:,:],
                                "intrinsic_M": model_input["intrinsics"][i,:,:].cpu().numpy(),
                                "unit_ray_array_2D": model_input["unit_ray"][i,:,:]}

                costV = warp_homo.est_swp_volume_v4( \
                        feat_img_ref,
                        feat_imgs_src,
                        d_candi, Rs_src, ts_src,
                        cam_intrinsics,
                        self.sigma_soft_max,
                        feat_dist = 'L2')
                # [1,128,64,96]

                cost_volumes.append(costV)

            cost_volumes = torch.cat(cost_volumes, dim=0)  # [4 128 64 96]
        # No warp
        else:
            cost_volumes = feat_imgs_all[:,-1,:-3,:,:]

        # Warp raw feature?
        warped_features = []
        for i in range(0, bsize):

            Rs_src = model_input["src_cam_poses"][i,:, :3,:3]
            ts_src = model_input["src_cam_poses"][i,:, :3,3]

            orig_feature = feat_raw_all[i,:,:,:,:].unsqueeze(0)

            cam_intrinsics = {"intrinsic_M_cuda": model_input["intrinsics"][i,:,:],
                              "intrinsic_M": model_input["intrinsics"][i,:,:].cpu().numpy(),
                              "unit_ray_array_2D": model_input["unit_ray"][i,:,:]}

            warped_feature = warp_homo.warp_feature(orig_feature, d_candi, Rs_src, ts_src, cam_intrinsics)

            warped_features.append(warped_feature)

        warped_features = torch.cat(warped_features, dim=0)

        # Refinement (3D Conv here or not)
        costv_out0 = self.conv0( cost_volumes )
        costv_out1 = self.conv0_1( costv_out0)
        costv_out2 = self.conv0_2( costv_out1)

        # Ensure log like
        BV = F.log_softmax(costv_out2, dim=1)

        # Return BV and primary image features (in the future return others too for flow?)
        last_features = [feat_imgs_all[:,-1,:-3, :,:], feat_imgs_layer_1[:,-1,:,:,:]]
        first_features = [feat_imgs_all[:,0,:-3, :,:], feat_imgs_layer_1[:,0,:,:,:]]
        return BV, cost_volumes, last_features, first_features, warped_features

    def forward_int(self, model_input):

        if self.nmode == "default":
            # Encoder
            BV_cur, cost_volumes, d_net_features, _ = self.forward_encoder(model_input)
            d_net_features.append(model_input["rgb"][:, -1, :, :, :])
            # [B, 128, 64, 96] - has log on it [[B,64,64,96] [B,32,128,192] [B,3,256,384]]

            # Decoder
            BV_cur_refined = self.base_decoder(torch.exp(BV_cur), img_features=d_net_features)
            # [B,128,256,384]

            return {"output": [BV_cur], "output_refined": [BV_cur_refined], "flow": None, "flow_refined": None}

        elif self.nmode == "exp2":
            # Encoder
            BV_cur, cost_volumes, d_net_features, _ = self.forward_encoder(model_input)
            d_net_features.append(model_input["rgb"][:,-1,:,:,:])
            # 64 in feature Dim depends on the command line arguments
            # [B, 128, 64, 96] - has log on it [[B,64,64,96] [B,32,128,192] [B,3,256,384]]

            # Create GT DPV from Depthmap / LIDAR
            tofuse_dpv = img_utils.gen_dpv_withmask(model_input["dmaps"], model_input["masks"], model_input["d_candi"], 0.3)

            # Fuse Data
            fused_dpv = torch.exp(BV_cur + torch.log(tofuse_dpv))
            fused_dpv = fused_dpv / torch.sum(fused_dpv, dim=1).unsqueeze(1)
            fused_dpv = torch.clamp(fused_dpv, img_utils.epsilon, 1.)
            BV_cur_fused = torch.log(fused_dpv)

            # Make sure size is still correct here!
            BV_cur_refined = self.base_decoder(fused_dpv, img_features=d_net_features)
            # [B,128,256,384]

            return {"output": [BV_cur_fused, BV_cur], "output_refined": [BV_cur_refined], "flow": None, "flow_refined": None}

        elif self.nmode == "exp3":
            # Encoder
            BV_cur, cost_volumes, last_features, first_features, warped_features = self.forward_exp(model_input)
            last_features.append(model_input["rgb"][:, -1, :, :, :])

            # Volume
            comb_volume = torch.cat([BV_cur.unsqueeze(1), warped_features], dim=1)
            BV_cur_upd = self.based_3d(comb_volume)

            # Decoder
            BV_cur_refined = self.base_decoder(torch.exp(BV_cur_upd), img_features=last_features)
            # [B,128,256,384]

            return {"output": [BV_cur, BV_cur_upd], "output_refined": [BV_cur_refined], "flow": None, "flow_refined": None}

        elif self.nmode == "exp7":
            # Encoder
            BV_cur, cost_volumes, last_features, first_features, warped_features = self.forward_exp(model_input)
            last_features.append(model_input["rgb"][:, -1, :, :, :])

            # Prev Output
            if model_input["prev_output"] is None:
                prev_output = torch.zeros(BV_cur.unsqueeze(1).shape).to(BV_cur.device) + 1./float(self.D)
            else:
                prev_output = model_input["prev_output"].unsqueeze(1)

            # Volume
            comb_volume = torch.cat([BV_cur.unsqueeze(1), prev_output, warped_features], dim=1)
            BV_resi = self.based_3d(comb_volume, prob=False)
            BV_cur_upd = F.log_softmax(BV_cur + BV_resi, dim=1)

            # Decoder
            BV_cur_refined = self.base_decoder(torch.exp(BV_cur_upd), img_features=last_features)

            return {"output": [BV_cur, BV_cur_upd], "output_refined": [BV_cur_refined], "flow": None, "flow_refined": None}

        elif self.nmode == "exp7_lc":
            # Encoder
            BV_cur, cost_volumes, last_features, first_features, warped_features = self.forward_exp(model_input)
            last_features.append(model_input["rgb"][:, -1, :, :, :])

            # Prev Output
            if model_input["prev_output"] is None:
                prev_output = torch.log(torch.zeros(BV_cur.unsqueeze(1).shape).to(BV_cur.device) + 1./float(self.D))
            else:
                prev_output = model_input["prev_output"].unsqueeze(1)

            # Prev LC Prob
            if self.cfg["eval"]:
                ignore_prev_lc = False
            else:
                use_lc_prob = self.cfg.lc.use_lc_prob
                ignore_prev_lc = random.random() > use_lc_prob

            # Prev LC
            if model_input["prev_lc"] is None or ignore_prev_lc:
                prev_lc = torch.log(torch.zeros(BV_cur.unsqueeze(1).shape).to(BV_cur.device) + 1./float(self.D))
            else:
                prev_lc = model_input["prev_lc"].unsqueeze(1)

            # Volume
            comb_volume = torch.cat([BV_cur.unsqueeze(1), prev_output, prev_lc, warped_features], dim=1)
            BV_resi = self.based_3d(comb_volume, prob=False)
            BV_cur_upd = F.log_softmax(BV_cur + BV_resi, dim=1)

            # Decoder
            BV_cur_refined = self.base_decoder(torch.exp(BV_cur_upd), img_features=last_features)

            # LC
            if self.algo_lc is not None:
                BV_lc, score = self.lc_process(BV_cur_refined.detach().clone(), model_input, self.algo_lc, mode="high", viz=False, iterations=self.cfg.lc.iterations, planner=self.cfg.lc.planner, params=self.cfg.lc.params)
                return {"output": [BV_cur, BV_cur_upd], "output_refined": [BV_cur_refined], "output_lc": BV_lc, "flow": None, "flow_refined": None}
            else:
                return {"output": [BV_cur, BV_cur_upd], "output_refined": [BV_cur_refined], "flow": None, "flow_refined": None}

        elif self.nmode == "lc":
            # Encoder
            BV_cur, cost_volumes, d_net_features, _ = self.forward_encoder(model_input)
            d_net_features.append(model_input["rgb"][:, -1, :, :, :])

            # Prev Output
            if model_input["prev_output"] is None:
                prev_output = torch.log(torch.zeros(BV_cur.unsqueeze(1).shape).to(BV_cur.device) + 1./float(self.D))
            else:
                prev_output = model_input["prev_output"].unsqueeze(1)

            # Prev LC Prob
            if self.cfg["eval"]:
                ignore_prev_lc = False
            else:
                use_lc_prob = self.cfg.lc.use_lc_prob
                ignore_prev_lc = random.random() > use_lc_prob

            # Prev LC
            if model_input["prev_lc"] is None or ignore_prev_lc:
                prev_lc = torch.log(torch.zeros(BV_cur.unsqueeze(1).shape).to(BV_cur.device) + 1./float(self.D))
            else:
                prev_lc = model_input["prev_lc"].unsqueeze(1)

            # Volume
            comb_volume = torch.cat([BV_cur.unsqueeze(1), prev_output, prev_lc, d_net_features[0].unsqueeze(1)], dim=1)
            BV_resi = self.based_3d(comb_volume, prob=False)
            BV_cur_upd = F.log_softmax(BV_cur + BV_resi, dim=1)

            # Decoder
            BV_cur_refined = self.base_decoder(torch.exp(BV_cur_upd), img_features=d_net_features)

            # LC
            if self.real_lc is not None and self.algo_lc is not None and model_input["camside"] == "left":
                NOTWORKING
                BV_lc, score = self.lc_process_real(BV_cur_refined.detach().clone(), model_input, self.algo_lc, self.real_lc, viz=True, iterations=self.cfg.lc.iterations, planner=self.cfg.lc.planner, params=self.cfg.lc.params)
                return {"output": [BV_cur, BV_cur_upd], "output_refined": [BV_cur_refined], "output_lc": BV_lc, "flow": None, "flow_refined": None}
            elif self.algo_lc is not None:
                BV_lc, score = self.lc_process(BV_cur_refined.detach().clone(), model_input, self.algo_lc, mode="high", viz=False, iterations=self.cfg.lc.iterations, planner=self.cfg.lc.planner, params=self.cfg.lc.params)
                return {"output": [BV_cur, BV_cur_upd], "output_refined": [BV_cur_refined], "output_lc": BV_lc, "flow": None, "flow_refined": None}
            else:
                return {"output": [BV_cur, BV_cur_upd], "output_refined": [BV_cur_refined], "flow": None, "flow_refined": None}

            pass

        else:
            raise Exception("Nmode wrong")

        pass

    def lc_process_real(self, BV_cur_all, model_input, algo_lc, real_lc, iterations=5, viz=False, score=False, planner="default", params=None):
        # Iterate each batch
        final_fused = []
        unc_scores_all = []
        for b in range(0, model_input["dmaps"].shape[0]):
            # Extract
            intr = model_input["intrinsics_up"][b, :, :]
            dmap = F.interpolate(model_input["dmaps"][b,:,:].unsqueeze(0).unsqueeze(0), scale_factor=4, mode='nearest').squeeze(0)
            mask = F.interpolate(model_input["masks"][b,:,:,:].unsqueeze(0), scale_factor=4, mode='nearest')
            img = model_input["rgb"][b, -1, :, :, :]
            feat_z_tensor = model_input["sweep_arr_tensor"][b, :, :, :, 0]
            feat_int_tensor = model_input["sweep_arr_tensor"][b, :, :, :, 1]
            # Remove Nan?
            feat_z_tensor[torch.isnan(feat_z_tensor)] = 1000

            # Expand it
            feat_int_tensor = F.interpolate(feat_int_tensor.unsqueeze(0), size=[feat_int_tensor.shape[1]*2, feat_int_tensor.shape[2]*2], mode='bicubic').squeeze(0)
            feat_z_tensor = F.interpolate(feat_z_tensor.unsqueeze(0), size=[feat_z_tensor.shape[1]*2, feat_z_tensor.shape[2]*2], mode='bicubic').squeeze(0)

            # True Depth
            true_depth = dmap.squeeze(0).cpu().numpy()

            # DPV Truth
            if viz or score:
                true_dpv = img_utils.gen_dpv_withmask(dmap, mask, algo_lc.d_candi, 0.3)
                unc_field_truth, _ = img_utils.gen_ufield(true_dpv, algo_lc.d_candi, intr.squeeze(0), BV_log=False, cfg=self.cfg)

                # Make Unc Field Truth (RGB) (LC)
                algo_lc.fw_large.load_flowfield()
                unc_field_truth_lc = algo_lc.fw_large.preprocess(unc_field_truth.squeeze(0), algo_lc.d_candi, algo_lc.d_candi_up)
                unc_field_truth_lc = algo_lc.fw_large.transformZTheta(unc_field_truth_lc, algo_lc.d_candi_up, algo_lc.d_candi_up, "transform_" + "large").unsqueeze(0)
                unc_field_truth_lc[:,:,0:50] = np.nan
                unc_field_truth_lc[:,:,-50:-1] = np.nan
                unc_field_truth = unc_field_truth_lc

            # Upsample
            BV_cur = BV_cur_all[b, :, :, :].unsqueeze(0)
            final = BV_cur.detach().clone()
            if final.shape[1] != algo_lc.expand_A:
                final = img_utils.upsample_dpv(final, N=algo_lc.expand_A, BV_log=True)

            # # Simulate a mid point spread start            
            # iterations = 20
            # final = torch.log(img_utils.gen_dpv_withmask(final[:,0,:,:]*0+15, final[:,0,:,:].unsqueeze(0)*0+1, algo_lc.d_candi, 10.0))
            # if final.shape[1] != algo_lc.expand_A:
            #     final = img_utils.upsample_dpv(final, N=algo_lc.expand_A, BV_log=True)

            # Bayesian Iterations
            unc_scores = []
            for i in range(0, iterations):

                # Generate UField
                unc_field_predicted, debugmap = img_utils.gen_ufield(final, algo_lc.d_candi, intr.squeeze(0), BV_log=True, cfg=self.cfg)

                # rgb_debug = img_utils.torchrgb_to_cv2(img)
                # rgb_debug[:,:,0] += debugmap[0,:,:].detach().cpu().numpy()
                # import cv2
                # cv2.imshow("rgb", rgb_debug)
                # cv2.waitKey(0)
                            
                # Score
                if score:
                    unc_score = img_utils.compute_unc_rmse(unc_field_truth, unc_field_predicted, algo_lc.d_candi)
                    unc_scores.append(unc_score.item())

                # Plan
                print("START")
                if planner == "default":
                    lc_paths, field_visual = algo_lc.plan_default_high(unc_field_predicted.squeeze(0), params)
                elif planner == "m1":
                    lc_paths, field_visual = algo_lc.plan_m1_high(unc_field_predicted.squeeze(0), params)
                elif planner == "sweep":
                    lc_paths, field_visual = algo_lc.plan_sweep_high(unc_field_predicted.squeeze(0), params)
                print("END")

                # Generate DPV
                lc_DPVs = []
                for lc_path in lc_paths:
                    # Simulated Measurement
                    depth_lc = np.ones((feat_int_tensor.shape[1], feat_int_tensor.shape[2])).astype(np.float32)*1000
                    output_lc, thickness_lc = real_lc.lightcurtain_large.get_return(depth_lc, lc_path, True)
                    output_lc[np.isnan(output_lc[:, :, 0])] = 0
                    thickness_lc[np.isnan(thickness_lc[:, :])] = 0

                    # Sampling intensities from real sensor
                    sampling_depth = torch.tensor(output_lc[:,:,2]).to(final.device)
                    inds = torch.argmin(torch.abs(sampling_depth.reshape(1, sampling_depth.shape[0], sampling_depth.shape[1]) - feat_z_tensor), dim=0)
                    result = torch.gather(feat_int_tensor, 0, inds.unsqueeze(0)).squeeze(0)
                    output_lc[:,:,3] = result.cpu().numpy()

                    # Transform to LC
                    sensed_arr = real_lc.transform_measurement(output_lc, thickness_lc)

                    # Gen DPV
                    #peak_img = torch.clamp(datum.nir_warped_tensor.squeeze(0) + 0.2, 0, 1)
                    peak_img = None
                    # lc_DPV = self.real_lc.gen_lc_dpv_approx(sensed_arr, 5) # May have to pass values here
                    lc_DPV = self.real_lc.gen_lc_dpv_true(sensed_arr, 2, peak_img) # May have to pass values here
                    # Add
                    lc_DPVs.append(lc_DPV)

                # # Viz
                if viz:
                    import cv2
                    unc_field_truth_image = unc_field_truth[0,:,:].cpu().numpy()
                    unc_field_truth_image = cv2.resize(unc_field_truth_image, (field_visual[:,:,2].shape[1],field_visual[:,:,2].shape[0]))
                    field_visual[:,:,2] = unc_field_truth_image*3
                    field_visual = cv2.flip(field_visual, 0)
                    cv2.imshow("field_visual", field_visual)
                    #cv2.imshow("final_depth", final_depth.squeeze(0).cpu().numpy()/100)
                    print("WAIT")
                    cv2.waitKey(0)
                    print("OK")

                # Keep Renormalize
                curr_dist = torch.clamp(torch.exp(final), img_utils.epsilon, 1.)

                # Update
                for lcdpv in lc_DPVs:
                    lcdpv[torch.isnan(lcdpv)] = 0. # Check why this is happening
                    lcdpv = torch.clamp(lcdpv, img_utils.epsilon, 1.)
                    curr_dist = curr_dist * lcdpv
                    curr_dist = curr_dist / torch.sum(curr_dist, dim=1).unsqueeze(1)

                # Spread
                for i in range(0, params.spread_iter):
                    curr_dist = img_utils.spread_dpv_hack(curr_dist, params.spread_n)

                # Keep Renormalize
                curr_dist = torch.clamp(curr_dist, img_utils.epsilon, 1.)

                # Back to Log space
                final = torch.log(curr_dist)

            if final.shape[1] != BV_cur.shape[1]:
                final = img_utils.upsample_dpv(final, N=BV_cur.shape[1], BV_log=True)
            final_fused.append(final)
            unc_scores_all.append(unc_scores)

        return torch.cat(final_fused, dim=0), unc_scores_all

    def lc_process(self, BV_cur_all, model_input, lc, mode="low", iterations=5, viz=False, score=False, planner="default", params=None):
        # Iterate each batch
        final_fused = []
        unc_scores_all = []
        for b in range(0, model_input["dmaps"].shape[0]):
            if mode == "high":
                intr = model_input["intrinsics_up"][b, :, :]
                dmap = F.interpolate(model_input["dmaps"][b,:,:].unsqueeze(0).unsqueeze(0), scale_factor=4, mode='nearest').squeeze(0)
                mask = F.interpolate(model_input["masks"][b,:,:,:].unsqueeze(0), scale_factor=4, mode='nearest')
                img = model_input["rgb"][b, -1, :, :, :]

                # # Special case for ilim due to higher res lidar
                # if "ilim" in self.cfg.data.exp_name:
                #     dmap = model_input["dmaps_up"][b,:,:].unsqueeze(0)
                #     # dmap = F.interpolate(dmap.unsqueeze(0), scale_factor=0.5, mode='nearest').squeeze(0)
                #     # dmap = F.interpolate(dmap.unsqueeze(0), scale_factor=2, mode='nearest').squeeze(0)

            elif mode == "low":
                intr = model_input["intrinsics"][b, :, :]
                dmap = model_input["dmaps"][b,:,:].unsqueeze(0)
                mask = model_input["masks"][b,:,:,:].unsqueeze(0)
                img_refined = model_input["rgb"][b, -1, :, :, :]
                img = F.interpolate(img_refined.unsqueeze(0), scale_factor=0.25, mode='bilinear').squeeze(0)

            # True Depth
            true_depth = dmap.squeeze(0).cpu().numpy()

            # DPV Truth
            if viz or score:
                true_dpv = img_utils.gen_dpv_withmask(dmap, mask, lc.d_candi, 0.3)
                unc_field_truth, _ = img_utils.gen_ufield(true_dpv, lc.d_candi, intr.squeeze(0), BV_log=False, cfg=self.cfg)

            # Upsample
            BV_cur = BV_cur_all[b, :, :, :].unsqueeze(0)
            final = BV_cur.detach().clone()
            if final.shape[1] != lc.expand_A:
                final = img_utils.upsample_dpv(final, N=lc.expand_A, BV_log=True)

            # # Simulate a mid point spread start            
            # iterations = 20
            # final = torch.log(img_utils.gen_dpv_withmask(final[:,0,:,:]*0+10, final[:,0,:,:].unsqueeze(0)*0+1, lc.d_candi, 6.0))
            # if final.shape[1] != lc.expand_A:
            #     final = img_utils.upsample_dpv(final, N=lc.expand_A, BV_log=True)

            # Bayesian Iterations
            unc_scores = []
            for i in range(0, iterations):

                # Generate UField
                unc_field_predicted, debugmap = img_utils.gen_ufield(final, lc.d_candi, intr.squeeze(0), BV_log=True, cfg=self.cfg)

                # rgb_debug = img_utils.torchrgb_to_cv2(img)
                # rgb_debug[:,:,0] += debugmap[0,:,:].detach().cpu().numpy()
                # import cv2
                # cv2.imshow("rgb", rgb_debug)
                # cv2.waitKey(1)

                # # Add epsilon and renormalize in case some are nan
                # unc_field_predicted[torch.isnan(unc_field_predicted)] = 0.
                # unc_field_predicted += img_utils.epsilon
                # unc_field_predicted = unc_field_predicted / torch.sum(unc_field_predicted, axis=1)

                # Score
                if score:
                    unc_score = img_utils.compute_unc_rmse(unc_field_truth, unc_field_predicted, lc.d_candi)
                    unc_scores.append(unc_score.item())

                # Plan
                if mode == "high":
                    if planner == "default":
                        lc_paths, field_visual = lc.plan_default_high(unc_field_predicted.squeeze(0), params)
                    elif planner == "m1":
                        lc_paths, field_visual = lc.plan_m1_high(unc_field_predicted.squeeze(0), params)
                    elif planner == "sweep":
                        lc_paths, field_visual = lc.plan_sweep_high(unc_field_predicted.squeeze(0), params)
                elif mode == "low":
                    if planner == "default":
                        lc_paths, field_visual = lc.plan_default_low(unc_field_predicted.squeeze(0), params)
                    elif planner == "m1":
                        lc_paths, field_visual = lc.plan_m1_low(unc_field_predicted.squeeze(0), params)

                # Sensing
                lc_DPVs = []
                lc_pts = []
                for lc_path in lc_paths:
                    if mode == "high":
                        lc_DPV, lc_pt, _ = lc.sense_high(true_depth, lc_path, viz)
                    elif mode == "low":
                        lc_DPV, _ = lc.sense_low(true_depth, lc_path)
                    lc_DPVs.append(lc_DPV)
                    lc_pts.append(lc_pt)

                # 3D
                if viz:
                    if self.viz is not None:
                        dmap_truth = model_input["dmaps"][b,:,:].unsqueeze(0)
                        dmap_truth = img_utils.cull_depth(dmap_truth, model_input["intrinsics"][b,:,:], 3)
                        for pts in lc_pts:
                            self.viz.addCloud(img_utils.lcoutput_to_cloud(pts), 2)
                            self.viz.addCloud(img_utils.tocloud(dmap_truth, img_utils.demean(F.interpolate(model_input["rgb"][b, -1, :, :, :].unsqueeze(0), scale_factor=0.25, mode='bilinear').squeeze(0)), model_input["intrinsics"][b,:,:], rgbr=[255,255,0]), 4)
                            self.viz.swapBuffer()
                        self.viz.addCloud(img_utils.lcoutput_to_cloud(lc_pts[0]), 2)
                        self.viz.addCloud(img_utils.tocloud(dmap_truth, img_utils.demean(F.interpolate(model_input["rgb"][b, -1, :, :, :].unsqueeze(0), scale_factor=0.25, mode='bilinear').squeeze(0)), model_input["intrinsics"][b,:,:], rgbr=[255,255,0]), 4)
                        self.viz.swapBuffer()

                # # Viz
                if viz:
                    import cv2
                    unc_field_truth_image = unc_field_truth[0,:,:].cpu().numpy()
                    unc_field_truth_image = cv2.resize(unc_field_truth_image, (field_visual[:,:,2].shape[1],field_visual[:,:,2].shape[0]))
                    field_visual[:,:,2] = unc_field_truth_image*3
                    field_visual = cv2.flip(field_visual, 0)
                    cv2.imshow("field_visual", field_visual)
                    #cv2.imshow("final_depth", final_depth.squeeze(0).cpu().numpy()/100)
                    cv2.waitKey(0)

                # Keep Renormalize
                curr_dist = torch.clamp(torch.exp(final), img_utils.epsilon, 1.)

                # # Update
                # curr_dist_log = torch.log(curr_dist)
                # for lcdpv in lc_DPVs:
                #     lcdpv = torch.clamp(lcdpv, img_utils.epsilon, 1.)
                #     curr_dist_log += torch.log(lcdpv)
                # curr_dist = torch.exp(curr_dist_log)
                # curr_dist = curr_dist / torch.sum(curr_dist, dim=1).unsqueeze(1)

                # Update
                for lcdpv in lc_DPVs:
                    lcdpv[torch.isnan(lcdpv)] = 0. # Check why this is happening
                    lcdpv = torch.clamp(lcdpv, img_utils.epsilon, 1.)
                    curr_dist = curr_dist * lcdpv
                    curr_dist = curr_dist / torch.sum(curr_dist, dim=1).unsqueeze(1)

                # Spread
                for i in range(0, params.spread_iter):
                    curr_dist = img_utils.spread_dpv_hack(curr_dist, params.spread_n)

                # Keep Renormalize
                curr_dist = torch.clamp(curr_dist, img_utils.epsilon, 1.)

                # Back to Log space
                final = torch.log(curr_dist)

            if final.shape[1] != BV_cur.shape[1]:
                final = img_utils.upsample_dpv(final, N=BV_cur.shape[1], BV_log=True)
            final_fused.append(final)
            unc_scores_all.append(unc_scores)

        return torch.cat(final_fused, dim=0), unc_scores_all
        
    def forward(self, inputs):
        # Setup Algo LC if setup
        if self.algo_lc is not None:
            if not self.algo_lc.initialized:
                if "algo_lc_config" in self.cfg.lc:
                    algo_lc_config = np.load(self.cfg.lc.algo_lc_config,  allow_pickle=True).item()
                    algo_lc_config = self.algo_lc.expand_params(algo_lc_config, self.cfg, 128, 128, "_algo")
                    algo_lc_config["device"] = inputs[0]["rgb"].device
                    self.algo_lc.init(algo_lc_config)
                else:
                    lc_params = self.algo_lc.gen_params_from_model_input(inputs[0])
                    lc_params = self.algo_lc.expand_params(lc_params, self.cfg, 64, 128)
                    self.algo_lc.init(lc_params)
        # Setup Real LC if setup
        if self.real_lc is not None:
            if not self.real_lc.initialized:
                if "real_lc_config" in self.cfg.lc:
                    real_lc_config = np.load(self.cfg.lc.real_lc_config,  allow_pickle=True).item()
                    real_lc_config = self.real_lc.expand_params(real_lc_config, self.cfg, 128, 128, "_real")
                    self.real_lc.init(real_lc_config)

        # Network
        outputs = []
        for input in inputs:
            outputs.append(self.forward_int(input))
        return outputs

class DefaultModel(nn.Module):
    def __init__(self, cfg, id):
        super(DefaultModel, self).__init__()
        self.cfg = cfg
        self.id = id
        self.conv_1x1 = nn.Sequential(conv(3, 32, kernel_size=3, stride=1, dilation=0, padding=1),
                                      nn.MaxPool2d(2),
                                      conv(32, self.cfg.var.ndepth, kernel_size=3, stride=1, dilation=0, padding=1),
                                      nn.MaxPool2d(2),
                                      #nn.BatchNorm2d(64)
                                      )

    def set_viz(self, viz):
        self.viz = viz

    def num_parameters(self):
        return sum(
            [p.data.nelement() if p.requires_grad else 0 for p in self.parameters()])

    def init_weights(self):
        for layer in self.named_modules():
            if isinstance(layer, nn.Conv2d):
                nn.init.kaiming_normal_(layer.weight)
                if layer.bias is not None:
                    nn.init.constant_(layer.bias, 0)

            elif isinstance(layer, nn.ConvTranspose2d):
                nn.init.kaiming_normal_(layer.weight)
                if layer.bias is not None:
                    nn.init.constant_(layer.bias, 0)

    def forward_int(self, input):
        images = input["rgb"][:, -1, :, :, :]
        output = self.conv_1x1(images)
        output_refined = F.interpolate(output, None, 4.)
        output_lsm = F.log_softmax(output, dim=1)
        output_refined_lsm = F.log_softmax(output_refined, dim=1)
        return {"output": [output_lsm], "output_refined": [output_refined_lsm], "flow": None, "flow_refined": None}

    def forward(self, inputs):
        outputs = []
        for input in inputs:
            outputs.append(self.forward_int(input))
        return outputs