from distributed_model import *

### utils fuction for CNN
def conv_layer_bn(in_channels: int, out_channels: int, activation: nn.Module, stride: int=1, bias: bool=False) -> nn.Module:
    conv = nn.Conv2d(in_channels, out_channels, kernel_size = 3, stride = stride, bias = bias, padding = 1)
    bn = nn.BatchNorm2d(out_channels)
    if activation == None:
        return nn.Sequential(conv, bn)
    return nn.Sequential(conv, bn, activation)

def conv_1x1_bn(in_channels: int, out_channels: int, activation: nn.Module, stride: int=1, bias: bool=False) -> nn.Module:
    conv = nn.Conv2d(in_channels, out_channels, kernel_size = 1, stride = stride, bias = bias)
    bn = nn.BatchNorm2d(out_channels)
    if activation == None:
        return nn.Sequential(conv, bn)
    return nn.Sequential(conv, bn, activation)

class CNNLayer(nn.Module):
    def __init__(self, conv:nn.Module, flatten_size:int, 
                 lab_dim:int, lr:float, out_dim:int=None,
                 ae_cri='mse', ae_act=[nn.Sigmoid(),nn.Sigmoid()] ):
        super().__init__()
        
        self.enc = ENC(None, conv=conv, out_dim=flatten_size, lab_dim=lab_dim, f='cnn',)
        if out_dim == None:
            self.ae = AE(lab_dim, lab_dim, cri=ae_cri, act=ae_act)
        else:
            self.ae = AE(out_dim, lab_dim, cri=ae_cri, act=ae_act)
    
        self.ae_opt = torch.optim.Adam(self.ae.parameters(), lr=lr)
        self.enc_opt = torch.optim.Adam(self.enc.parameters(), lr=lr)
        self.ae_sche = ReduceLROnPlateau(self.ae_opt, mode="max", factor=0.5, patience=5, min_lr=0.00001)
        self.enc_sche = ReduceLROnPlateau(self.enc_opt, mode="max", factor=0.5, patience=5, min_lr=0.00001)

    def forward(self, x, y):

        self.ae_opt.zero_grad()
        enc_y , ae_loss = self.ae(y)
        if self.training:
            ae_loss.backward()
            self.ae_opt.step()
    
        self.enc_opt.zero_grad()
        tgt = enc_y.detach()
        enc_x, enc_loss, _, _ = self.enc(x, tgt)
        if self.training:
            enc_loss.backward()
            self.enc_opt.step()

        return enc_x.detach(), enc_y.detach(), ae_loss, enc_loss

class cnn_alModel(alModel):
    def __init__(self, num_layer:int, l1_dim:int, class_num:int, lab_dim:int):
        super().__init__(num_layer, l1_dim, class_num, lab_dim)
        self.conf_type = "max"
        self.data_distribution = [0 for _ in range(self.num_layer)]
    def forward(self, x, y):
        layer_loss = []
        y = torch.nn.functional.one_hot(y, self.class_num).float().to(y.device)
        x_out = x
        y_out = y
        #print("L0 Xin", x_out.size())
        #print("L0 Yin", y_out.size())
        # forward function also update
        for idx,layer in enumerate(self.layers):
            x_out, y_out, ae_out, as_out = layer(x_out, y_out)
            layer_loss.append([ae_out.item(), as_out.item()])
            #print(f"L{idx} Xout", x_out.size())
            #print(f"L{idx} Yout", y_out.size())
        return layer_loss
    
    ### func for inference_adapt
    def layer_forward(self, x, idx):
        x_out = self.layers[idx].enc.f(x) 
        return x_out
    
    def bridge_return(self, x, len_path):
        x_out = x 
        y_out = self.layers[len_path].enc.b(x_out)
        for idx in reversed(range(len_path+1)):
            y_out = self.layers[idx].ae.h(y_out)
        return y_out

    @torch.no_grad()
    def inference_adapt(self, x, threshold=0.1, max_depth=None):
        #######################################################
        # x: batch of input sample
        # Samples with (entropy > threshold) will go to next layer
        # max_depth: the max depth of layer a sample will go throught
        #######################################################
        if max_depth==None:
           max_depth = self.num_layer
        assert 1 <= max_depth and max_depth <= self.num_layer
        
        entr = torch.ones(x.size(0), requires_grad=False).cuda() #[batch_size] 
        pred = torch.zeros((x.size(0), self.class_num), requires_grad=False).cuda() #[batch_size, label_size]
        total_remain_idx = torch.ones(x.size(0), dtype=torch.bool).cuda() #[batch_size]
        
        
        x_out = x
        for idx in range(self.num_layer):
            
            # f forward
            x_out = self.layer_forward(x_out, idx)
            # return form b/h
            y_out = self.bridge_return(x_out, idx)

            y_entr = confidence(y_out)

            if self.conf_type == "entropy":
                remain_idx = y_entr>threshold
            elif self.conf_type == "max":    
                remain_idx = y_entr<threshold
            
            entr[total_remain_idx] = y_entr
            pred[total_remain_idx,:] = y_out
            total_remain_idx[total_remain_idx==True] = remain_idx
            
            # remained X forward to next layer
            x_out = x_out[remain_idx,:]
            
            if x_out.size(0) == 0 or idx+1 == max_depth:  
                self.data_distribution[idx] += len(remain_idx)
                break
            else:
                self.data_distribution[idx] += torch.sum((~remain_idx).int()).item()
        #print(data_distribution)
        return pred, entr
    
    @torch.no_grad()
    def inference(self, x, len_path=None):
        # full path inference by default
        if len_path == None:
            len_path = self.num_layer
            
        assert 1 <= len_path and len_path <= self.num_layer
        
        x_out = x
        for idx in range(len_path):
            x_out = self.layers[idx].enc.f(x_out)    
            
        y_out = self.layers[len_path-1].enc.b(x_out)
        
        for idx in reversed(range(len_path)):
            y_out = self.layers[idx].ae.h(y_out)
            
        return y_out

class CNN_AL(cnn_alModel):    
    def __init__(self, num_layer:int, l1_dim:int, lr:float, class_num:int, lab_dim:int=128):
        super().__init__(num_layer, l1_dim, class_num, lab_dim)
        
        self.num_layer = 4
        layers = ModuleList([])
        ### L0
        conv1 = nn.Sequential(nn.Conv2d(3, 32, kernel_size = 3, stride = 1, bias = True, padding = 1), nn.ReLU())
        conv2 = nn.Sequential(nn.Conv2d(32, 32, kernel_size = 3, stride = 1, bias = True, padding = 1), nn.ReLU())
        block_1 = nn.Sequential(conv1, conv2)
        layer = CNNLayer(block_1, 32*32*32, lab_dim, out_dim=class_num, lr=lr,
                         ae_cri='ce', ae_act=[nn.Sigmoid(), nn.Sigmoid()] )
        layers.append(layer)
        ### L1
        conv3 = nn.Sequential(nn.Conv2d(32, 32, kernel_size = 3, stride = 1, bias = True, padding = 1), nn.ReLU())
        conv4 = nn.Sequential(nn.Conv2d(32, 32, kernel_size = 3, stride = 2, bias = True, padding = 1), nn.ReLU())
        block_2 = nn.Sequential(conv3, conv4)
        layer = CNNLayer(block_2, 32*16*16, lab_dim, lr=lr,
                         ae_cri='mse', ae_act=[nn.Sigmoid(), None] )
        layers.append(layer)
        ### L2
        conv5 = nn.Sequential(nn.Conv2d(32, 64, kernel_size = 3, stride = 1, bias = True, padding = 1), nn.ReLU())
        conv6 = nn.Sequential(nn.Conv2d(64, 64, kernel_size = 3, stride = 1, bias = True, padding = 1), nn.ReLU())
        block_3 = nn.Sequential(conv5, conv6)
        layer = CNNLayer(block_3, 64*16*16, lab_dim, lr=lr,
                         ae_cri='mse', ae_act=[nn.Sigmoid(), None] )
        layers.append(layer)
        ### L3
        conv7 = nn.Sequential(nn.Conv2d(64, 64, kernel_size = 3, stride = 1, bias = True, padding = 1), nn.ReLU())
        conv8 = nn.Sequential(nn.Conv2d(64, 64, kernel_size = 3, stride = 2, bias = True, padding = 1), nn.ReLU())
        block_4 = nn.Sequential(conv7, conv8)
        layer = CNNLayer(block_4, 64*8*8, lab_dim, lr=lr,
                         ae_cri='mse', ae_act=[nn.Sigmoid(), None] )
        layers.append(layer)
        
        self.layers = layers     
        
class VGG_AL(cnn_alModel):    
    def __init__(self, num_layer:int, l1_dim:int, lr:float, class_num:int, lab_dim:int=500):
        super().__init__(num_layer, l1_dim, class_num, lab_dim)
        
        layer_cfg = {0:[128, 256, "M"], 1:[256, 512, "M"], 2:[512, "M"], 3:[512, "M"]}
        self.shape = 32
        self.features = 3
        self.num_layer = 4
        
        layers = ModuleList([])
        ### h has act_func, out_dim=class_num
        ### L0
        self.shape //= 2
        block_1 = self._make_layer(layer_cfg[0])
        layer = CNNLayer(block_1, self.shape*self.shape*256, lab_dim, out_dim=class_num, lr=lr,
                         ae_cri='ce', ae_act=[nn.Sigmoid(), nn.Sigmoid()] )
        layers.append(layer)
        ### L1
        self.shape //= 2
        block_2 = self._make_layer(layer_cfg[1])
        layer = CNNLayer(block_2, self.shape*self.shape*512, lab_dim, lr=lr,
                         ae_cri='mse', ae_act=[nn.Sigmoid(), nn.Sigmoid()] )
        layers.append(layer)
        ### L2
        self.shape //= 2
        block_3 = self._make_layer(layer_cfg[2])
        layer = CNNLayer(block_3, self.shape*self.shape*512, lab_dim, lr=lr,
                         ae_cri='mse', ae_act=[nn.Sigmoid(), nn.Sigmoid()] )
        layers.append(layer)
        ### L3
        self.shape //= 2
        block_4 = self._make_layer(layer_cfg[3])
        layer = CNNLayer(block_4, self.shape*self.shape*512, lab_dim, lr=lr,
                         ae_cri='mse', ae_act=[nn.Sigmoid(), nn.Sigmoid()] )
        layers.append(layer)
        
        self.layers = layers     
    
    def _make_layer(self, channel_size):
        layers = []
        for dim in channel_size:
            if dim == "M":
                layers.append(nn.MaxPool2d(2, stride=2))
                #self.size/=2
            else:
                layers.append(conv_layer_bn(self.features, dim, nn.ReLU(), bias=False))
                self.features = dim
        return nn.Sequential(*layers)
    
""" 修改自: https://github.com/batuhan3526/ResNet50_on_Cifar_100_Without_Transfer_Learning """
class BasicBlock(nn.Module):
    expansion = 1

    def __init__(self, in_channels, out_channels, stride=1):
        super().__init__()
        self.conv1 = conv_layer_bn(in_channels, out_channels, nn.LeakyReLU(inplace=True), stride, False)
        self.conv2 = conv_layer_bn(out_channels, out_channels, None, 1, False)
        self.relu = nn.LeakyReLU(inplace=True)

        self.shortcut = nn.Sequential()

        # the shortcut output dimension is not the same with residual function
        if stride != 1:
            #self.shortcut = conv_layer_bn(in_channels, out_channels, None, stride, False)
            self.shortcut = conv_1x1_bn(in_channels, out_channels, None, stride, False)

    def forward(self, x):
        out = self.conv1(x)
        out = self.conv2(out)
        out = self.relu(out + self.shortcut(x))
        return out

class resnet18_AL(cnn_alModel):
    def __init__(self, num_layer:int, l1_dim:int, lr:float, class_num:int, lab_dim:int=500):
        super().__init__(num_layer, l1_dim, class_num, lab_dim)
        
        self.shape = 32
        layers = ModuleList([])
        
        ### h has act_func, out_dim=class_num
        ### L0
        conv1 = conv_layer_bn(3, 64, nn.LeakyReLU(inplace=True), 1, False)
        conv2_x = self._make_layer(64, 64, [1, 1])
        layer = CNNLayer(nn.Sequential(conv1, conv2_x), int(64 * self.shape * self.shape), lab_dim, out_dim=class_num, lr=lr,
                         ae_cri='ce', ae_act=[nn.Sigmoid(), nn.Sigmoid()] )
        layers.append(layer)
        ### L1
        conv3_x = self._make_layer(64, 128, [2, 1])
        self.shape /= 2
        layer = CNNLayer(conv3_x, int(128 * self.shape * self.shape), lab_dim, lr=lr,
                         ae_cri='mse', ae_act=[nn.Sigmoid(),nn.Sigmoid()] )
        layers.append(layer)
        ### L2
        conv4_x = self._make_layer(128, 256, [2, 1])        
        self.shape /= 2
        layer = CNNLayer(conv4_x, int(256 * self.shape * self.shape), lab_dim, lr=lr,
                         ae_cri='mse', ae_act=[nn.Sigmoid(),nn.Sigmoid()] )
        layers.append(layer)
        ### L3
        conv5_x = self._make_layer(256, 512, [2, 1])
        self.shape /= 2
        layer = CNNLayer(conv5_x, int(512 * self.shape * self.shape), lab_dim, lr=lr,
                         ae_cri='mse', ae_act=[nn.Sigmoid(),nn.Sigmoid()] )
        layers.append(layer)
        
        self.layers = layers  
        
    def _make_layer(self, in_channels, out_channels, strides):
        layers = []
        cur_channels = in_channels
        for stride in strides:
            layers.append(BasicBlock(cur_channels, out_channels, stride))
            cur_channels = out_channels

        return nn.Sequential(*layers)
    

class CNN_AL_side(CNN_AL):    
    def __init__(self, num_layer:int, l1_dim:int, lr:float, class_num:int, lab_dim:int=128):
        super().__init__(num_layer, l1_dim, lr, class_num, lab_dim)
        
        self.num_layer = 4
        self.side_layers = ModuleList([None])
        ### L1
        conv1 = nn.Sequential(nn.Conv2d(3, 32, kernel_size = 3, stride = 1, bias = False, padding = 1), nn.ReLU())
        self.side_layers.append(conv1)
        ### L2
        conv2 = nn.Sequential(nn.Conv2d(3, 32, kernel_size = 3, stride = 2, bias = False, padding = 1), nn.ReLU())
        self.side_layers.append(conv2)
        ### L2
        conv3 = nn.Sequential(nn.Conv2d(3, 64, kernel_size = 3, stride = 2, bias = False, padding = 1), nn.ReLU())
        self.side_layers.append(conv3)
    
    def forward(self, x, y):
        layer_loss = []
        y = torch.nn.functional.one_hot(y, self.class_num).float().to(y.device)
        x_out = x
        y_out = y
        #print("L0 Xin", x_out.size())
        #print("L0 Yin", y_out.size())
        # forward function also update
        for idx,layer in enumerate(self.layers):
            if idx != 0:
                side = self.side_layers[idx](x)
                #print(f"L{idx} Sout", side.size())
                x_out = x_out + side
            x_out, y_out, ae_out, as_out = layer(x_out, y_out)
            layer_loss.append([ae_out.item(), as_out.item()])
            #print(f"L{idx} Xout", x_out.size())
            #print(f"L{idx} Yout", y_out.size())
        return layer_loss
    
    ### func for inference_adapt
    def layer_forward(self, x, idx):
        x_out = self.layers[idx].enc.f(x) 
        return x_out
    
    def bridge_return(self, x, len_path):
        x_out = x 
        y_out = self.layers[len_path].enc.b(x_out)
        for idx in reversed(range(len_path+1)):
            y_out = self.layers[idx].ae.h(y_out)
        return y_out

    @torch.no_grad()
    def inference_adapt(self, x, threshold=0.1, max_depth=None):
        #######################################################
        # x: batch of input sample
        # Samples with (entropy > threshold) will go to next layer
        # max_depth: the max depth of layer a sample will go throught
        #######################################################
        if max_depth==None:
           max_depth = self.num_layer
        assert 1 <= max_depth and max_depth <= self.num_layer
        
        entr = torch.ones(x.size(0), requires_grad=False).cuda() #[batch_size] 
        pred = torch.zeros((x.size(0), self.class_num), requires_grad=False).cuda() #[batch_size, label_size]
        total_remain_idx = torch.ones(x.size(0), dtype=torch.bool).cuda() #[batch_size]
        
        x_out = x
        for idx in range(self.num_layer):
            if idx >= max_depth: break
            # f forward
            if idx != 0:
                side = self.side_layers[idx](x)
                x_out = x_out + side
            x_out = self.layer_forward(x_out, idx)
            # return form b/h
            y_out = self.bridge_return(x_out, idx)

            y_entr = confidence(y_out)

            if self.conf_type == "entropy":
                remain_idx = y_entr>threshold
            elif self.conf_type == "max":    
                remain_idx = y_entr<threshold
            
            entr[total_remain_idx] = y_entr
            pred[total_remain_idx,:] = y_out
            
            
            
            total_remain_idx[total_remain_idx==True] = remain_idx
            
            x_out = x_out[remain_idx,:]
            x = x[remain_idx,:]
            if x_out.size(0) == 0: break
    
        return pred, entr
    
class VGG_AL_side(VGG_AL):    
    def __init__(self, num_layer:int, l1_dim:int, lr:float, class_num:int, lab_dim:int=128):
        super().__init__(num_layer, l1_dim, lr, class_num, lab_dim)
        
        self.num_layer = 4
        self.side_layers = ModuleList([None])
        ### L1
        conv1 = nn.Sequential(nn.Conv2d(3, 256, kernel_size = 3, stride = 2, bias = False, padding = 1), nn.ReLU())
        self.side_layers.append(conv1)
        ### L2
        conv2 = nn.Sequential(nn.Conv2d(3, 512, kernel_size = 3, stride = 4, bias = False, padding = 1), nn.ReLU())
        self.side_layers.append(conv2)
        ### L2
        conv3 = nn.Sequential(nn.Conv2d(3, 512, kernel_size = 3, stride = 8, bias = False, padding = 1), nn.ReLU())
        self.side_layers.append(conv3)
    
    def forward(self, x, y):
        layer_loss = []
        y = torch.nn.functional.one_hot(y, self.class_num).float().to(y.device)
        x_out = x
        y_out = y
        #print("L0 Xin", x_out.size())
        #print("L0 Yin", y_out.size())
        # forward function also update
        for idx,layer in enumerate(self.layers):
            if idx != 0:
                side = self.side_layers[idx](x)
                #print(f"L{idx} Sout", side.size())
                x_out = x_out + side
            x_out, y_out, ae_out, as_out = layer(x_out, y_out)
            layer_loss.append([ae_out.item(), as_out.item()])
            #print(f"L{idx} Xout", x_out.size())
            #print(f"L{idx} Yout", y_out.size())
        return layer_loss
    
    ### func for inference_adapt
    def layer_forward(self, x, idx):
        x_out = self.layers[idx].enc.f(x) 
        return x_out
    
    def bridge_return(self, x, len_path):
        x_out = x 
        y_out = self.layers[len_path].enc.b(x_out)
        for idx in reversed(range(len_path+1)):
            y_out = self.layers[idx].ae.h(y_out)
        return y_out

    @torch.no_grad()
    def inference_adapt(self, x, threshold=0.1, max_depth=None):
        #######################################################
        # x: batch of input sample
        # Samples with (entropy > threshold) will go to next layer
        # max_depth: the max depth of layer a sample will go throught
        #######################################################
        if max_depth==None:
           max_depth = self.num_layer
        assert 1 <= max_depth and max_depth <= self.num_layer
        
        entr = torch.ones(x.size(0), requires_grad=False).cuda() #[batch_size] 
        pred = torch.zeros((x.size(0), self.class_num), requires_grad=False).cuda() #[batch_size, label_size]
        total_remain_idx = torch.ones(x.size(0), dtype=torch.bool).cuda() #[batch_size]
        
        x_out = x
        for idx in range(self.num_layer):
            if idx >= max_depth: break
            # f forward
            if idx != 0:
                side = self.side_layers[idx](x)
                x_out = x_out + side
            x_out = self.layer_forward(x_out, idx)
            # return form b/h
            y_out = self.bridge_return(x_out, idx)

            y_entr = confidence(y_out)

            if self.conf_type == "entropy":
                remain_idx = y_entr>threshold
            elif self.conf_type == "max":    
                remain_idx = y_entr<threshold
            
            entr[total_remain_idx] = y_entr
            pred[total_remain_idx,:] = y_out
            
            
            
            total_remain_idx[total_remain_idx==True] = remain_idx
            
            x_out = x_out[remain_idx,:]
            x = x[remain_idx,:]
            if x_out.size(0) == 0: break
    
        return pred, entr
    
class resnet_AL_side(resnet18_AL):    
    def __init__(self, num_layer:int, l1_dim:int, lr:float, class_num:int, lab_dim:int=128):
        super().__init__(num_layer, l1_dim, lr, class_num, lab_dim)
        
        self.num_layer = 4
        self.side_layers = ModuleList([None])
        ### L1
        conv1 = nn.Sequential(nn.Conv2d(3, 64, kernel_size = 3, stride = 1, bias = False, padding = 1), nn.ReLU())
        self.side_layers.append(conv1)
        ### L2
        conv2 = nn.Sequential(nn.Conv2d(3, 128, kernel_size = 3, stride = 2, bias = False, padding = 1), nn.ReLU())
        self.side_layers.append(conv2)
        ### L2
        conv3 = nn.Sequential(nn.Conv2d(3, 256, kernel_size = 3, stride = 4, bias = False, padding = 1), nn.ReLU())
        self.side_layers.append(conv3)
    
    def forward(self, x, y):
        layer_loss = []
        y = torch.nn.functional.one_hot(y, self.class_num).float().to(y.device)
        x_out = x
        y_out = y

        # forward function also update
        for idx,layer in enumerate(self.layers):
            if idx != 0:
                side = self.side_layers[idx](x)
                x_out = x_out + side
            x_out, y_out, ae_out, as_out = layer(x_out, y_out)
            layer_loss.append([ae_out.item(), as_out.item()])
        return layer_loss
    
    ### func for inference_adapt
    def layer_forward(self, x, idx):
        x_out = self.layers[idx].enc.f(x) 
        return x_out
    
    def bridge_return(self, x, len_path):
        x_out = x 
        y_out = self.layers[len_path].enc.b(x_out)
        for idx in reversed(range(len_path+1)):
            y_out = self.layers[idx].ae.h(y_out)
        return y_out

    @torch.no_grad()
    def inference_adapt(self, x, threshold=0.1, max_depth=None):
        #######################################################
        # x: batch of input sample
        # Samples with (entropy > threshold) will go to next layer
        # max_depth: the max depth of layer a sample will go throught
        #######################################################
        if max_depth==None:
           max_depth = self.num_layer
        assert 1 <= max_depth and max_depth <= self.num_layer
        
        entr = torch.ones(x.size(0), requires_grad=False).cuda() #[batch_size] 
        pred = torch.zeros((x.size(0), self.class_num), requires_grad=False).cuda() #[batch_size, label_size]
        total_remain_idx = torch.ones(x.size(0), dtype=torch.bool).cuda() #[batch_size]
        
        x_out = x
        for idx in range(self.num_layer):
            # f forward
            if idx != 0:
                side = self.side_layers[idx](x)
                x_out = x_out + side
            x_out = self.layer_forward(x_out, idx)
            # return form b/h
            y_out = self.bridge_return(x_out, idx)

            y_entr = confidence(y_out)

  
            remain_idx = y_entr<threshold
            
            entr[total_remain_idx] = y_entr
            pred[total_remain_idx,:] = y_out
            total_remain_idx[total_remain_idx==True] = remain_idx
            
            x_out = x_out[remain_idx,:]
            x = x[remain_idx,:]
            
            if x_out.size(0) == 0 or idx+1 == max_depth:  
                self.data_distribution[idx] += len(remain_idx)
                break
            else:
                self.data_distribution[idx] += torch.sum((~remain_idx).int()).item()
        
    
        return pred, entr
  

class CNN(nn.Module):
    def __init__(self, class_num):
        super(CNN, self).__init__()
        num_neurons = 300
        self.conv1 = nn.Sequential(nn.Conv2d(3, 32, kernel_size = 3, stride = 1, bias = True, padding = 1), nn.ReLU())
        self.conv2 = nn.Sequential(nn.Conv2d(32, 32, kernel_size = 3, stride = 1, bias = True, padding = 1), nn.ReLU())
        self.conv3 = nn.Sequential(nn.Conv2d(32, 32, kernel_size = 3, stride = 1, bias = True, padding = 1), nn.ReLU())
        
        self.conv4 = nn.Sequential(nn.Conv2d(32, 32, kernel_size = 3, stride = 2, bias = True, padding = 1), nn.ReLU())
        
        self.conv5 = nn.Sequential(nn.Conv2d(32, 64, kernel_size = 3, stride = 1, bias = True, padding = 1), nn.ReLU())
        self.conv6 = nn.Sequential(nn.Conv2d(64, 64, kernel_size = 3, stride = 1, bias = True, padding = 1), nn.ReLU())
        self.conv7 = nn.Sequential(nn.Conv2d(64, 64, kernel_size = 3, stride = 1, bias = True, padding = 1), nn.ReLU())
        
        self.conv8 = nn.Sequential(nn.Conv2d(64, 64, kernel_size = 3, stride = 2, bias = True, padding = 1), nn.ReLU())
        
        
        #self.fc = nn.Sequential(Flatten(), nn.Linear(4096, 5*num_neurons), nn.Sigmoid(), nn.Linear(5*num_neurons, class_num))
        self.fc = nn.Sequential(Flatten(), nn.Linear(4096, 5*num_neurons), nn.Sigmoid(), nn.Linear(5*num_neurons, num_neurons),
                                nn.Linear(num_neurons, num_neurons),nn.Linear(num_neurons, num_neurons),nn.Linear(num_neurons, num_neurons),nn.Linear(num_neurons, class_num),)
        
        self.ce = nn.CrossEntropyLoss()
        self.class_num = class_num
        
    def forward(self, x, y):
        out = self.conv1(x)
        out = self.conv2(out)
        out = self.conv3(out)
        out = self.conv4(out)
        out = self.conv5(out)
        out = self.conv6(out)
        out = self.conv7(out)
        out = self.conv8(out)
        
        out = self.fc(out)
        
        if self.training:
            return self.ce(out, y)
        else:
            return out.detach()
        
    def summary(self):
        print("model:",self)  
        total_params = 0
        for name, parameter in self.named_parameters():
            if not parameter.requires_grad: continue
            params = parameter.numel()
            total_params+=params
            print(name, params)
        print(f"Total Trainable Params: {total_params}")
        return total_params
    
class resnet18(nn.Module):

    def __init__(self, class_num):
        super().__init__()
        num_neurons = 500
        self.class_num = class_num
        self.conv1 = conv_layer_bn(3, 64, nn.LeakyReLU(inplace=True), 1, False)

        self.conv2_x = self._make_layer(64, 64, [1, 1])
        self.conv3_x = self._make_layer(64, 128, [2, 1])
        self.conv4_x = self._make_layer(128, 256, [2, 1])
        self.conv5_x = self._make_layer(256, 512, [2, 1])
        self.avg_pool = nn.AdaptiveAvgPool2d((1, 1))
        #self.fc = nn.Linear(512, class_num)
        self.fc = nn.Sequential(Flatten(), nn.Linear(512, 5*num_neurons), nn.Sigmoid(), nn.Linear(5*num_neurons, num_neurons),
                                nn.Linear(num_neurons, num_neurons),nn.Linear(num_neurons, num_neurons),nn.Linear(num_neurons, num_neurons),nn.Linear(num_neurons, class_num),)
        
        self.ce = nn.CrossEntropyLoss()


    def _make_layer(self, in_channels, out_channels, strides):
        layers = []
        cur_channels = in_channels
        for stride in strides:
            layers.append(BasicBlock(cur_channels, out_channels, stride))
            cur_channels = out_channels
        return nn.Sequential(*layers)

    def forward(self, x, y=None):
        output = self.conv1(x)
        output = self.conv2_x(output)
        output = self.conv3_x(output)
        output = self.conv4_x(output)
        output = self.conv5_x(output)
        output = self.avg_pool(output)
        output = output.view(output.size(0), -1)
        output = self.fc(output)
        if self.training:
            return self.ce(output, y)
        else:
            return output
        
    def summary(self):
        print("model:",self)  
        total_params = 0
        for name, parameter in self.named_parameters():
            if not parameter.requires_grad: continue
            params = parameter.numel()
            total_params+=params
            print(name, params)
        print(f"Total Trainable Params: {total_params}")
        return total_params
    
class VGG(nn.Module):
    def __init__(self, class_num):
        super(VGG, self).__init__()
        
        num_neurons = 500
        self.class_num = class_num
        self.in_channel = 3
        self.ce = nn.CrossEntropyLoss()

        self.layer1 = self._make_layer([128, 256, 'M'])
        self.layer2 = self._make_layer([256, 512, 'M'])
        self.layer3 = self._make_layer([512, 'M'])
        self.layer4 = self._make_layer([512, 'M'])

        #self.fc = nn.Sequential(Flatten(), nn.Linear(2048, 2500), nn.Sigmoid(), nn.Linear(2500, class_num))
        self.fc = nn.Sequential(Flatten(), nn.Linear(2048, 5*num_neurons), nn.Sigmoid(), nn.Linear(5*num_neurons, num_neurons),
                                nn.Linear(num_neurons, num_neurons),nn.Sigmoid(),
                                nn.Linear(num_neurons, num_neurons),nn.Sigmoid(),
                                nn.Linear(num_neurons, num_neurons),nn.Sigmoid(),
                                nn.Linear(num_neurons, class_num),)
        
        
    def _make_layer(self, channel_size: list):
        layers = []
        for dim in channel_size:
            if dim == 'M':
                layers.append(nn.MaxPool2d(2, stride=2))
            else:
                layers.append(conv_layer_bn(self.in_channel, dim, nn.ReLU()))
                self.in_channel = dim
        return nn.Sequential(*layers)

    def _make_linear_layer(self, in_features, out_features):
        return nn.Sequential(nn.Linear(in_features, out_features, bias=True), nn.BatchNorm1d(out_features), nn.ReLU())
    
    def forward(self, x, y):

        out = self.layer1(x)
        out = self.layer2(out)
        out = self.layer3(out)
        out = self.layer4(out)

        out = self.fc(out)


        if self.training:
            loss = self.ce(out, y)


        if self.training:
            return loss
        else:
            return out.detach()

    def summary(self):
        print("model:",self)  
        total_params = 0
        for name, parameter in self.named_parameters():
            if not parameter.requires_grad: continue
            params = parameter.numel()
            total_params+=params
            print(name, params)
        print(f"Total Trainable Params: {total_params}")
        return total_params
    

