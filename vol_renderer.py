import torch
from typing import Tuple,Optional
import numpy as np
import h5py as h5
import cv2
import matplotlib.pyplot as plt
import torch.nn as nn
from encoder import *
import time
from tqdm import tqdm
from helper import *
class NeRF(nn.Module):
  r"""
  Neural radiance fields module.
  """
  def __init__(
    self,
    d_input: int = 3,
    n_layers: int = 8,
    d_filter: int = 256,
    skip: Tuple[int] = (4,),
    d_viewdirs: Optional [int] = None
  ):
    super().__init__()
    self.d_input = d_input
    self.skip = skip
    self.act = nn.ReLU()
    self.d_viewdirs = d_viewdirs
    self.sigmoid = nn.Sigmoid()
    # Create model layers
    self.layers = nn.ModuleList(
      [nn.Linear(self.d_input, d_filter)] +
      [nn.Linear(d_filter + self.d_input, d_filter) if i in skip \
       else nn.Linear(d_filter, d_filter) for i in range(n_layers - 1)]
    )

    # Bottleneck layers
    if self.d_viewdirs is not None:
        # If using viewdirs, split alpha and RGB
        self.alpha_out = nn.Linear(d_filter, 1)
        self.rgb_filters = nn.Linear(d_filter, d_filter)
        self.branch = nn.Linear(d_filter + self.d_viewdirs, d_filter // 2)
        self.output = nn.Linear(d_filter // 2, 3)
    else:
        # If no viewdirs, use simpler output
        self.output = nn.Linear(d_filter, 4)
  
  def forward(
    self,
    x: torch.Tensor,
    viewdirs: Optional[torch.Tensor] = None
  ) -> torch.Tensor:
    r"""
    Forward pass with optional view direction.
    """

    # Cannot use viewdirs if instantiated with d_viewdirs = None
    if self.d_viewdirs is None and viewdirs is not None:
        raise ValueError('Cannot input x_direction if d_viewdirs was not given.')

    # Apply forward pass up to bottleneck
    x_input = x
    for i, layer in enumerate(self.layers):
        x = self.act(layer(x))
        if i in self.skip:
            x = torch.cat([x, x_input], dim=-1)
    #   print(f"{i}_layer_in",x.shape)

    # Apply bottleneck
    if self.d_viewdirs is not None:
        # Split alpha from network output
        alpha = self.alpha_out(x)
        alpha = self.sigmoid(alpha)
        # Pass through bottleneck to get RGB
        x = self.rgb_filters(x)
        x = torch.concat([x, viewdirs], dim=-1)
        x = self.act(self.branch(x))
        x = self.output(x)
        
        x = self.act(x)
        # Concatenate alphas to output
        x = torch.concat([x, alpha], dim=-1)
    else:
        # Simple output
        x = self.output(x)
    return x

class Volume_Renderer():
    def __init__(self,H,W,K,near=0.,far=1.,device=None,
                Pos_encode:Optional [PositionalEncoder]=None,
                Dir_encode:Optional [PositionalEncoder]=None,
                max_dim=1024,sigma_val=torch.as_tensor(1),mu=torch.as_tensor(0),
                use_sdf:Optional[bool]=False,
                var_model: Optional[nn.Module] = None,):
        self.H=H
        self.W=W
        self.K=K
        self.near=near
        self.far=far
        self.coods_x,self.coords_y= torch.meshgrid(torch.arange(W,device=device), torch.arange(H,device=device), indexing='xy')
        self.device=device
        if device is None:
            self.device="cuda" if torch.cuda.is_available() else "cpu"
        self.Pos_encode=Pos_encode
        self.Dir_encode=Dir_encode
        self.grid_size=max_dim//4
        self.bool_grid=torch.ones((self.grid_size,self.grid_size,self.grid_size),device=self.device,dtype=torch.bool)
        self.sigma_val=sigma_val.to(self.device)
        self.mu=mu.to(self.device)
        self.epislon=1e-5
        self.tmp_arr=torch.zeros((self.grid_size,self.grid_size,self.grid_size),device=self.device,dtype=torch.int8)
        self.reset_mask=False
        self.use_sdf=use_sdf
        self.var_model=var_model
    
    def update_grid(self,points:torch.Tensor,alpha:torch.Tensor,):
        # alpha=alpha_in.reshape(-1)
        # points=points_in.reshape(-1,3)
        points=(points-self.mu)/self.sigma_val
        points=points*self.grid_size
        points=points.long()
        alpha[alpha<=0]=0
        # print("Alpha_sum:::",(alpha==0).sum())
        self.tmp_arr[points[...,0],points[...,1],points[...,2]]+=torch.ceil(alpha).int()
        
        if torch.sum(self.tmp_arr>0)==0:
            self.bool_grid[...]=True
        else:
            self.bool_grid[self.tmp_arr>0]=True
        # print("UPDATED_MASK_VALS:",(self.bool_grid<=0).sum())
        self.tmp_arr[self.tmp_arr>0]=0

    def get_mask(self,points:torch.Tensor)->torch.Tensor:
        # points=points_in.reshape(-1,3)
        points=(points-self.mu)/self.sigma_val
        points=points*(self.grid_size)
        points=points.long()
        mask=self.bool_grid[points[...,0],points[...,1],points[...,2]]
        # print("Points_shape,mask_shape",points.shape,mask.shape)
        return mask
    def vol_render(
            self,
            model:NeRF,
            rays_d:torch.Tensor,
            rays_o:torch.Tensor,
            num_samples=100,
            t:Optional [torch.Tensor]=None,
            update_mask=False,
            dir_norm:Optional [int]=1,
            hierarchical=True,
        )->Tuple[torch.Tensor,torch.Tensor]:
        # device="cuda" if torch.cuda.is_available() else "cpu"
        near=self.near
        far=self.far
        Pos_encode=self.Pos_encode
        Dir_encode=self.Dir_encode
        if rays_d.get_device()>=0:
            device="cuda"
        else:
            device="cpu"
        # print("DEVICE:!!",device)
        if t is None:
            t=strat_sampler(near,far,num_samples,device=device)
            # t=t[None,:,None]
        rays=rays_o[...,None,:]+rays_d[...,None,:]*t[None,:,None]
        # print("RAYS_SHAPE:",rays[...,0].max(),rays[...,1].max(),rays[...,2].max(),rays[...,0].min(),rays[...,1].min(),rays[...,2].min())
        orig_shape=rays.shape
        # print("RAYS_SHAPE:",rays.shape)
        # print("t_shape:",rays_o[:,None,:].shape)
        t1=0
        t1=time.time()
        if Pos_encode is not None and Dir_encode is not None:
            rays=rays.reshape(-1,3)
            rays_tmp=rays
            mask=self.get_mask(rays)

            dirs=rays_d[...,None,:].repeat(1,num_samples,1)
            dirs=dirs.reshape(-1,3)
            rays=Pos_encode(rays)
            # print("ENCODED_RAY SHAPE",rays.shape)
            dirs=Dir_encode(dirs)
            rays=rays.reshape(rays.shape[0],-1)
            dirs=dirs.reshape(dirs.shape[0],-1)
        elif Pos_encode is not None and Dir_encode is None:
            rays=rays.reshape(-1,3)
            rays_tmp=rays
            mask=self.get_mask(rays)
            dirs=rays_d[...,None,:].repeat(1,num_samples,1)
            dirs=dirs.reshape(-1,3)
            rays=Pos_encode(rays)
        else:
            print("ERROR: No positional encoding")
        t1=time.time()-t1

        t2=time.time()
        # print("Mask",mask.sum(),mask.shape)
        t2=time.time()-t2
        t3=time.time()
        if update_mask is True:
            model_out=model(rays,dirs)
            if self.reset_mask is True:
                self.bool_grid[...]=False
                self.reset_mask=False
            # self.update_grid(rays_tmp,model_out[...,3])
            sigma=model_out[...,3:4]
            rgb=model_out[...,0:3]
            sigma=sigma.reshape(orig_shape[0],orig_shape[1])
            rgb=rgb.reshape(orig_shape[0],orig_shape[1],-1)
        else:
        # model_out=model_out
            model_out=model(rays[mask],dirs[mask])
            # print("MASKED_MODEL_OUT",model_out.shape)
            sigma=torch.zeros((orig_shape[0]*orig_shape[1],1),device=self.device,dtype=model_out.dtype)
            rgb=torch.zeros((orig_shape[0]*orig_shape[1],3),device=self.device,dtype=model_out.dtype)
            sigma[mask]=model_out[...,3:4]
            rgb[mask]=model_out[...,0:3]
            # model_out=model(rays,dirs,mask=mask)
            # sigma=model_out[...,3:4]
            # rgb=model_out[...,0:3]
            sigma=sigma.reshape(orig_shape[0],orig_shape[1])
            rgb=rgb.reshape(orig_shape[0],orig_shape[1],-1)

        Cr,wts,norm=calc_color(t=t,rgb=rgb,sigma=sigma,dir_norm=dir_norm,use_sdf=self.use_sdf,var_model=self.var_model,rays=rays_tmp,model=model,encoder=Pos_encode,device=device)

        if hierarchical is True:
            rays_fine,t_fine=hierarchical_sampling(rays_o,rays_d,z_vals=t,weights=wts,n_samples=num_samples,tn=near,tf=far,device=device)
            orig_shape=rays_fine.shape
            if Pos_encode is not None and Dir_encode is not None:
                dirs_fine=rays_d[...,None,:].repeat(1,rays_fine.shape[-2],1)
                rays_fine=rays_fine.reshape(-1,3)
                dirs_fine=dirs_fine.reshape(-1,3)
                rays_fine=Pos_encode(rays_fine)
                dirs_fine=Dir_encode(dirs_fine)
                rays_fine=rays_fine.reshape(rays_fine.shape[0],-1)
                dirs_fine=dirs_fine.reshape(dirs_fine.shape[0],-1)

            model_out=model(rays_fine,dirs_fine)
            sigma_fine=model_out[...,3:4]
            rgb_fine=model_out[...,0:3]
            sigma_fine=sigma_fine.reshape(orig_shape[0],orig_shape[1])
            rgb_fine=rgb_fine.reshape(orig_shape[0],orig_shape[1],-1)
            Cf,_,norm=calc_color(t=t_fine,rgb=rgb_fine,sigma=sigma_fine,dir_norm=dir_norm,use_sdf=self.use_sdf,var_model=self.var_model,model=model,encoder=Pos_encode,device=device)
        else:
            Cf=Cr
        return Cr,Cf,norm


######################################################################
def make_batch(
        in_rays:torch.Tensor,
        batch_size: int,
    )->list:
    batches=[]
    for i in range(0,in_rays.shape[0],batch_size):
        batches.append(in_rays[i:i+batch_size].to('cpu'))
    return batches

if __name__=="__main__":
    # print(datacube.shape)
    device='cuda' if torch.cuda.is_available() else 'cpu'
    print("deivce:",device)
    # H=256
    # W=256

    K=torch.from_numpy(np.array([[1,0,0],[0,1,0],[0,0,1]])).to(device)
    # c2w=torch.randint(5,(4,4)).to(torch.float32).to(device)
    num_freq=10
    model=NeRF(d_input=3*num_freq*2,d_viewdirs=2*num_freq*2)
    model=model.to(device)

    data = np.load('tiny_nerf_data.npz')
    images = data['images']
    poses = data['poses']
    focal = data['focal']
    train_imgs=images[:-1,...]
    test_imgs=images[-1:-2:-1,...]
    train_pose=poses[:-1,...]
    test_pose=poses[-1:-2:-1,...]
    H,W,_=images.shape[1:]
    # print(images.shape,train_imgs.shape,test_imgs.shape)

    K[0,0]=torch.tensor(focal)
    K[1,1]=torch.tensor(focal)
    K[0,2]=W/2
    K[1,2]=H/2
    print("CAMERA SHAPES:",images.shape,poses.shape,focal)
    c2w=torch.from_numpy(np.array([[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]],dtype=np.float32)).to(device)
    c2w=torch.tensor(poses[0,...],device=device)
    print("c2w:",c2w)
    rays_o, rays_d,_=get_od(H,W,K,c2w)
    origins=rays_o.cpu().numpy()
    dirs=rays_d.cpu().numpy()

    print("MIN_MAX:",rays_d.min(),rays_d.max())
    C=vol_render(rays_o,rays_d,num_samples=100,far=50)
    print(C[0])
    C=C.reshape(H,W,3).cpu().numpy()
    plt.imshow(C)

    plt.show()
