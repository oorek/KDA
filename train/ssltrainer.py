from torchvision import transforms as T
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from torch.utils.data import DataLoader

import sys
sys.path.append('.')
from train.trainer import Trainer
from util import data
from util.random_augment import RandomAugment
import dataloader
from dataloader import SSLDataLoader


class SSLTrainer(Trainer):
    def init_transform(self):
        """
        Create data augmentation transformer for train and val
        :return Ttrain, Tval: pytorch transform. data transformer for pytorch dataset
        :return Tnorm: nn.Module for fast image normalization on GPU
        """
        # CIFAR-10: mean: [0.49139968, 0.48215841, 0.44653091], std: [0.24703223, 0.24348513, 0.26158784]
        # CIFAR-100: mean: [0.50707516, 0.48654887, 0.44091784], std: [0.26733429, 0.25643846, 0.27615047]
        # ImageNet: mean: [0.485, 0.456, 0.406], std: [0.229, 0.224, 0.225]
        # STL: mean: [0.44087802, 0.42790631, 0.38678794], std: [0.26826769, 0.26104504, 0.26866837]
        # mini-ImageNet: mean: [0.47872189, 0.44985512, 0.40134091], st: [0.27524031, 0.26572543, 0.28019405]
        Ttrain = RandomAugment(N=self.config['transform']['data_augment']['N'], # 2
                        M=self.config['transform']['data_augment']['M']) # 9
        if self.model.backbone == 'vit' or self.teacher.backbone == 'vit':
            Ttrain = T.Compose([Ttrain, T.Resize((224,224)), T.ToTensor()])
            Tval = T.Compose([T.Resize((224,224)), T.ToTensor()])
            Tsimple = T.Compose([T.RandomHorizontalFlip(),
                                T.RandomCrop(self.config['data']['shape'], padding=self.config['data']['shape']//16),
                                T.Resize((224,224)),
                                T.ToTensor()])   
        else:
            Ttrain = T.Compose([Ttrain, T.ToTensor()])
            Tval = T.ToTensor()
            Tsimple = T.Compose([T.RandomHorizontalFlip(),
                                T.RandomCrop(self.config['data']['shape'], padding=self.config['data']['shape']//16),
                                T.ToTensor()])

        if self.config['transform']['preprocess']['type'] == 'zca':
            Tnorm = data.ZCATransformer(self.config['transform']['preprocess']['config'])
        elif self.config['transform']['preprocess']['type'] == 'mean-std':
            Tnorm = data.MeanStdTransformer(mean=[0.50707516, 0.48654887, 0.44091784],
                                            std=[0.26826769, 0.26104504, 0.26866837])
        else:
            raise ValueError

        return Ttrain, Tval, Tsimple, Tnorm.to(self.default_device)

    def init_dataloader(self):
        Ttrain, Tval, Tsimple, Tnorm = self.init_transform()
        print(f'Source Domains: {self.config["data"]["src_domains"]}; Target Domains: {self.config["data"]["tgt_domains"]}')

        dset = getattr(dataloader, dataloader.supported_ssl_dsets[self.config['data']['dataset']]) # dataset

        d = dset.split_data(root_dir=self.config['data']['root_dir'],
                            tgt_domains=self.config['data']['tgt_domains'],
                            src_domains=self.config['data']['src_domains'],
                            r_val=self.config['data']['Nv'] if not self.args.omniscient else None, # val ratio
                            r_lab=self.config['data']['Nl'], # labeled dataset : 250
                            r_unlab=self.config['data']['Nu'], # unlabeled dataset : none
                            w_unlab=self.config['data']['Wu'], 
                            rand_seed=self.args.rand_seed,
                            r_data=self.config['data']['Nd'])

        xl, yl, xu, xv, yv, xt, yt = d #(250,32,32,3), (250,), (49750,32,32,3), (10000,32,32,3), (10000,), (10000,32,32,3), (10000)
        xl, yl = data.shuffle_data([xl, yl], self.args.rand_seed+1)
        xv, yv = data.shuffle_data([xv, yv], self.args.rand_seed+2)
        xt, yt = data.shuffle_data([xt, yt], self.args.rand_seed+3)
        xu, = data.shuffle_data([xu], self.args.rand_seed+4)

        K, shape = self.config['transform']['data_augment']['K'], self.config['data']['shape'] # 8, 32 
        # 8번 반복 augmentation한다는거같음
        if self.model.backbone == 'vit' or self.teacher.backbone == 'vit':
            dtrain_lab = dset(x=xl, y=yl, Taggr=Ttrain, Tsimp=Tsimple, K=K, shape=224) #개별적으로 데이터셋 만들고
            dtrain_unlab = dset(x=xu, y=None, Taggr=Ttrain, Tsimp=Tsimple, K=K, shape=224)
            dval = dset(x=xv, y=yv, Taggr=None, Tsimp=Tval, K=None, shape=224)
            dtest = dset(x=xt, y=yt, Taggr=None, Tsimp=Tval, K=None, shape=224)
        else:
            dtrain_lab = dset(x=xl, y=yl, Taggr=Ttrain, Tsimp=Tsimple, K=K, shape=shape) #개별적으로 데이터셋 만들고
            dtrain_unlab = dset(x=xu, y=None, Taggr=Ttrain, Tsimp=Tsimple, K=K, shape=shape)
            dval = dset(x=xv, y=yv, Taggr=None, Tsimp=Tval, K=None, shape=shape)
            dtest = dset(x=xt, y=yt, Taggr=None, Tsimp=Tval, K=None, shape=shape)

        bsl, bsu = self.config['train']['bsl'], self.config['train']['bsu'] # batch 64, 128
        loader_train = SSLDataLoader(dtrain_lab, dtrain_unlab, bsl, bsu, self.args.workers) # 데이터 로더는 한번에?, 안에서 분리하는듯
        loader_val = DataLoader(dval, batch_size=(bsl+bsu), shuffle=False, num_workers=self.args.workers)
        loader_test = DataLoader(dtest, batch_size=(bsl+bsu), shuffle=False, num_workers=self.args.workers)

        return loader_train, loader_val, loader_test, Ttrain, Tval, Tnorm

    def get_consistency_coeff(self):
        """
        Implement linear ramp-up policy of the consistency loss weight.
        :return alpha: consistency loss weight
        """
        if self.curr_iter >= self.config['train']['coeff_rampup']:
            return 1.0
        else:
            # 계수 뻥튀기
            # return math.exp(-5 * (1 - current_iter / self.config['train']['coeff_rampup']) ** 2)
            return self.curr_iter / self.config['train']['coeff_rampup']
