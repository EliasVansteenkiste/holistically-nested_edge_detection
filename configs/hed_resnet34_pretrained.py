import numpy as np
import torch
import torchvision
import torch.optim as optim
import torch.nn as nn
import torch.nn.functional as F
from collections import namedtuple
from functools import partial
from PIL import Image

import data_transforms
import data_iterators
import pathfinder
import utils
import app


restart_from_save = None
rng = np.random.RandomState(37148)

# transformations
p_transform = {'patch_size': (896, 896),
               'channels': 3}

# only lossless augmentations
p_augmentation = {
    'rot90_values': [0, 1, 2, 3],
    'flip': [0, 1]
}

# mean and std values for imagenet
mean = np.asarray([0.485, 0.456, 0.406])
mean = mean[:, None, None]
std = np.asarray([0.229, 0.224, 0.225])
std = std[:, None, None]


# data preparation function
def data_prep_fun(x, y, random_gen):

    # print(x.height, x.width)
    #
    # downscale_factor = 2
    # x = x.resize((x.width//downscale_factor, x.height//downscale_factor))
    # y = y.resize((y.width//downscale_factor, y.height//downscale_factor))

    x = np.asanyarray(x)
    y = np.asanyarray(y)

    x = np.swapaxes(x, 0, 2)
    x = np.swapaxes(x, 1, 2)
    x = (x / 255. - mean) / std
    x = x.astype(np.float32)

    y = y / 255.
    y = y[None, :, :]
    y = y.astype(np.float32)

    x, y = data_transforms.random_crop_x_y(x, y, p_transform['patch_size'][0], p_transform['patch_size'][1], random_gen)

    return x, y

def data_reverse_tf(x):
    x = 255 * (std * x + mean)
    x = np.clip(x, 0, 255)
    x = x.astype(int)
    x = np.swapaxes(x, 0, 2)
    x = np.swapaxes(x, 0, 1)
    return x


train_data_prep_fun = partial(data_prep_fun, random_gen=rng)
valid_data_prep_fun = partial(data_prep_fun, random_gen=np.random.RandomState(0))

# data iterators
batch_size = 8
nbatches_chunk = 1
chunk_size = batch_size * nbatches_chunk

# dataset1 = app.get_id_pairs('test_data/test1/trainA', 'test_data/test1_hed/trainA')
dataset1 = app.get_id_pairs('ir2day_3108/trainA', 'hed_ir2day_3108/trainA')
dataset2 = app.get_id_pairs('ir2day_3108/trainB', 'hed_ir2day_3108/trainB')
img_id_pairs = [dataset1, dataset2]

id_pairs = app.train_val_test_split(img_id_pairs, train_fraction=.7, val_fraction=.15, test_fraction=.15)

bad_ids = []
id_pairs['train'] = [x for x in id_pairs['train'] if x not in bad_ids]
id_pairs['valid'] = [x for x in id_pairs['valid'] if x not in bad_ids]
id_pairs['test'] = [x for x in id_pairs['test'] if x not in bad_ids]

train_data_iterator = data_iterators.EdgeDataGenerator(mode='all',
                                                       batch_size=chunk_size,
                                                       img_id_pairs=id_pairs['train'],
                                                       data_prep_fun=train_data_prep_fun,
                                                       label_prep_fun=train_data_prep_fun,
                                                       rng=rng,
                                                       full_batch=True, random=True, infinite=True)

valid_data_iterator = data_iterators.EdgeDataGenerator(mode='all',
                                                       batch_size=chunk_size,
                                                       img_id_pairs=id_pairs['valid'],
                                                       data_prep_fun=valid_data_prep_fun,
                                                       label_prep_fun=valid_data_prep_fun,
                                                       rng=rng,
                                                       full_batch=False, random=False, infinite=False)

test_data_iterator = data_iterators.EdgeDataGenerator(mode='all',
                                                      batch_size=chunk_size,
                                                      img_id_pairs=id_pairs['test'],
                                                      data_prep_fun=valid_data_prep_fun,
                                                      label_prep_fun=valid_data_prep_fun,
                                                      rng=rng,
                                                      full_batch=False, random=False, infinite=False)

nchunks_per_epoch = train_data_iterator.nsamples // chunk_size
max_nchunks = nchunks_per_epoch * 40
print('max_nchunks', max_nchunks)

validate_every = int(1 * nchunks_per_epoch)
save_every = int(5 * nchunks_per_epoch)

learning_rate_schedule = {
    0: 5e-4,
    int(max_nchunks * 0.3): 2e-4,
    int(max_nchunks * 0.6): 1e-4,
    int(max_nchunks * 0.8): 3e-5,
    int(max_nchunks * 0.9): 1e-5
}


# models
class ResNet34(nn.Module):

    def __init__(self, block, layers, num_classes=1000):
        self.inplanes = 64
        super(ResNet34, self).__init__()
        self.conv1 = nn.Conv2d(3, 64, kernel_size=7, stride=2, padding=(35, 35),
                               bias=False)
        self.bn1 = nn.BatchNorm2d(64)
        self.relu = nn.ReLU(inplace=True)
        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)
        self.layer1 = self._make_layer(block, 64, layers[0])
        self.layer2 = self._make_layer(block, 128, layers[1], stride=2)
        self.layer3 = self._make_layer(block, 256, layers[2], stride=2)
        self.layer4 = self._make_layer(block, 512, layers[3], stride=2)
        self.avgpool = nn.AvgPool2d(7)
        self.fc_drop = nn.Dropout(p=0.75)
        self.fc = nn.Linear(512 * block.expansion, num_classes)

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                m.weight.data.normal_(0, (2. / n)**.5)
            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()

    def _make_layer(self, block, planes, blocks, stride=1):
        downsample = None
        if stride != 1 or self.inplanes != planes * block.expansion:
            downsample = nn.Sequential(
                nn.Conv2d(self.inplanes, planes * block.expansion,
                          kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm2d(planes * block.expansion),
            )

        layers = []
        layers.append(block(self.inplanes, planes, stride, downsample))
        self.inplanes = planes * block.expansion
        for i in range(1, blocks):
            layers.append(block(self.inplanes, planes))

        return nn.Sequential(*layers)

    # def forward(self, x):
    #     x = self.conv1(x)
    #     x = self.bn1(x)
    #     x = self.relu(x)
    #     x = self.maxpool(x)
    #
    #     x = self.layer1(x)
    #     x = self.layer2(x)
    #     x = self.layer3(x)
    #     x = self.layer4(x)
    #     # x = self.fc_drop(x4)
    #     # x = self.avgpool(x)
    #     # x = x.view(x.size(0), -1)
    #     # x = self.fc(x)
    #
    #     return x

    # def get_features(self, x):
    #     x = self.conv1(x)
    #     x = self.bn1(x)
    #     x = self.relu(x)
    #     x = self.maxpool(x)

    #     x = self.layer1(x)
    #     x = self.layer2(x)
    #     x = self.layer3(x)
    #     x = self.layer4(x)
    #     return x

    def forward(self, x):
        x = self.conv1(x)
        x = self.bn1(x)
        x0 = self.relu(x)
        x = self.maxpool(x0)

        x1 = self.layer1(x)
        x2 = self.layer2(x1)
        x3 = self.layer3(x2)
        x4 = self.layer4(x3)
        # x = self.fc_drop(x4)
        # x = self.avgpool(x)
        # x = x.view(x.size(0), -1)
        # x = self.fc(x)

        return x0, x1, x2, x3, x4


# class FeatureResNet34(nn.Module):
#     def __init__(self, gpu_ids, **kwargs):
#         super(FeatureResNet34, self).__init__()
#         self.gpu_ids = gpu_ids
#         self.resnet = ResNet34(torchvision.models.resnet.BasicBlock, [3, 4, 6, 3], **kwargs)
#         self.resnet.load_state_dict(torch.utils.model_zoo.load_url(torchvision.models.resnet.model_urls['resnet34']))
#         for param in self.resnet.parameters():
#             param.requires_grad = False
#
#     def forward(self, input):
#         if len(self.gpu_ids) and isinstance(input.data, torch.cuda.FloatTensor):
#             return nn.parallel.data_parallel(self.resnet, input, self.gpu_ids)
#         else:
#             return self.resnet(input)


class HEDResNet(nn.Module):
    def __init__(self, load_pretrained_resnet_weights):
        super(HEDResNet, self).__init__()
        self.score_dsn1 = nn.Conv2d(64, 1, kernel_size=1, stride=1, padding=0)
        self.score_dsn2 = nn.Conv2d(64, 1, kernel_size=1, stride=1, padding=0)
        self.score_dsn3 = nn.Conv2d(128, 1, kernel_size=1, stride=1, padding=0)
        self.score_dsn4 = nn.Conv2d(256, 1, kernel_size=1, stride=1, padding=0)
        self.score_dsn5 = nn.Conv2d(512, 1, kernel_size=1, stride=1, padding=0)

        self.upsample1 = nn.Upsample(scale_factor=2, mode='bilinear')
        self.upsample2 = nn.Upsample(scale_factor=4, mode='bilinear')
        self.upsample3 = nn.Upsample(scale_factor=8, mode='bilinear')
        self.upsample4 = nn.Upsample(scale_factor=16, mode='bilinear')
        self.upsample5 = nn.Upsample(scale_factor=32, mode='bilinear')

        self.crop = torch.nn.ReflectionPad2d(-32)

        self.drop = nn.Dropout(p=.5)

        self.cd1 = nn.Conv2d(1472, 512, kernel_size=1, stride=1, padding=0)
        self.cd2 = nn.Conv2d(512, 512, kernel_size=1, stride=1, padding=0)
        self.cd3 = nn.Conv2d(512, 1, kernel_size=1, stride=1, padding=0)

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                m.weight.data.normal_(0, (2. / n) ** .5)
            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()

        self.resnet = ResNet34(torchvision.models.resnet.BasicBlock, [3, 4, 6, 3])
        if load_pretrained_resnet_weights:
            self.resnet.load_state_dict(torch.utils.model_zoo.load_url(torchvision.models.resnet.model_urls['resnet34']))

    def forward(self, x):
        # print('x', x.size())

        c1, c2, c3, c4, c5 = self.resnet.forward(x)
        # print('c1', c1.size())
        # print('c2', c2.size())
        # print('c3', c3.size())
        # print('c4', c4.size())
        # print('c5', c5.size())

        s1 = self.score_dsn1(c1)
        s2 = self.score_dsn2(c2)
        s3 = self.score_dsn3(c3)
        s4 = self.score_dsn4(c4)
        s5 = self.score_dsn5(c5)

        s1 = self.upsample1(s1)
        s2 = self.upsample2(s2)
        s3 = self.upsample3(s3)
        s4 = self.upsample4(s4)
        s5 = self.upsample5(s5)

        # print('s1', s1.size())
        # print('s2', s2.size())
        # print('s3', s3.size())
        # print('s4', s4.size())
        # print('s5', s5.size())

        s1 = F.sigmoid(s1)
        s2 = F.sigmoid(s2)
        s3 = F.sigmoid(s3)
        s4 = F.sigmoid(s4)
        s5 = F.sigmoid(s5)

        out = 0.2 * s1 + 0.2 * s2 + 0.2 * s3 + 0.2 * s4 + 0.2 * s5

        # print('out', out.size())
        out = self.crop(out)

        # print('out', out.size())
        return out


def build_model():
    net = HEDResNet(load_pretrained_resnet_weights=True)
    return namedtuple('Model', ['l_out'])(net)


def _assert_no_grad(variable):
    assert not variable.requires_grad, \
        "nn criterions don't compute the gradient w.r.t. targets - please " \
        "mark these variables as volatile or not requiring gradients"


class SimpleBCELoss(nn.Module):
    def __init__(self, size_average=True):
        super(SimpleBCELoss, self).__init__()
        self.size_average = size_average

    def forward(self, input, target):
        _assert_no_grad(target)

        print('input', input.size(), 'target', target.size(), 'max', torch.max(target).data.cpu().numpy(), 'min', torch.min(target).data.cpu().numpy())

        return F.binary_cross_entropy(input, target, size_average = self.size_average)


class SimpleMSELoss(nn.Module):
    def __init__(self):
        super(SimpleMSELoss, self).__init__()

    def forward(self, input, target):
        _assert_no_grad(target)

        sq_err = (target - input) ** 2
        return torch.mean(sq_err)


class WeightedBCELoss(nn.Module):
    def __init__(self, size_average=True):
        super(WeightedBCELoss, self).__init__()
        self.size_average = size_average

    def forward(self, input, target):
        _assert_no_grad(target)

        beta = 1 - torch.mean(target)

        # target pixel = 1 -> weight beta
        # target pixel = 0 -> weight 1-beta
        weights = 1 - beta + (2 * beta - 1) * target

        return F.binary_cross_entropy(input, target, weights, self.size_average)


class WeightedMSELoss(nn.Module):
    def __init__(self):
        super(WeightedMSELoss, self).__init__()

    def forward(self, input, target):
        _assert_no_grad(target)

        # print('input', input.size())
        # print('target', target.size())

        err = (target - input)
        sq_err = err**2

        sign_err = torch.sign(err)
        is_pos_err = (sign_err + 1) / 2
        is_neg_err = (sign_err - 1) / -2

        edge_mass = torch.sum(target)
        empty_mass = torch.sum(1-target)
        total_mass = edge_mass + empty_mass

        weight_pos_err = empty_mass / total_mass
        weight_neg_err = edge_mass / total_mass

        pos_part = weight_pos_err * is_pos_err * sq_err
        neg_part = weight_neg_err * is_neg_err * sq_err

        weighted_sq_errs = neg_part + pos_part

        return torch.mean(weighted_sq_errs)


def build_objective():
    return WeightedMSELoss()


def build_objective2():
    return SimpleMSELoss()


def score(preds, gts):
    return app.cont_f_score(preds, gts)


def intermediate_valid_predictions(xs, gts, preds, pid, it_valid, n_save=10):
    path = pathfinder.METADATA_PATH + '/checkpoints/' + pid
    utils.auto_make_dir(path)
    pred_id = 0
    for batch_x, batch_gt, batch_pred in zip(xs, gts, preds):
        for x, gt, pred in zip(batch_x, batch_gt, batch_pred):
            if pred_id >= n_save:
                break
            # save pred
            pred = 255 * pred
            pred = pred.astype(int)
            app.save_image(pred[0], path + '/' + str(it_valid) + '_' + str(pred_id) + '_pred.jpg', mode='L')

            # save ground truth
            gt = 255 * gt
            gt = gt.astype(int)
            app.save_image(gt, path + '/' + str(it_valid) + '_' + str(pred_id) + '_real.jpg', mode='L')

            # save input
            data_reverse_tf(x)
            app.save_image(x, path + '/' + str(it_valid) + '_' + str(pred_id) + '_input.jpg', mode='RGB')

            pred_id += 1


# # updates
# def build_updates(model, learning_rate):
#     return optim.SGD(model.parameters(), lr=learning_rate, momentum=0.9, weight_decay=0.0002)

# updates
def build_updates(model, learning_rate):
    return optim.Adam(model.parameters(), lr=learning_rate)
