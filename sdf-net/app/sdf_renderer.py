# The MIT License (MIT)
#
# Copyright (c) 2021, NVIDIA CORPORATION.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the "Software"), to deal in
# the Software without restriction, including without limitation the rights to
# use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
# the Software, and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
# FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
# COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
# IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.


import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import math
import time

from PIL import Image
import numpy as np
import torch
import torch.nn as nn
from tqdm import tqdm
import moviepy.editor as mpy
from scipy.spatial.transform import Rotation as R
import pyexr

from lib.renderer import Renderer
from lib.models import *
from lib.options import parse_options
from lib.geoutils import sample_unif_sphere, sample_fib_sphere, normalized_slice
from lib.geometry import CubeMarcher

def write_exr(path, data):
    pyexr.write(path, data,
                channel_names={'normal': ['X','Y','Z'], 
                               'x': ['X','Y','Z'],
                               'view': ['X','Y','Z']},
                precision=pyexr.HALF)

class GyroidLattice(nn.Module):
    """Gyroid lattice for the given implicit neural geometry."""
    def __init__(self, sdf_net):
        """Constructor.
        Args:
            sdf_net (nn.Module): SDF network
        """
        super().__init__()
        self.sdf_net = sdf_net    
    
    def forward(self, x):
        """Evaluates uniform grid (N, 3) using gyroid implicit equation. Returns (N,) result."""
         # x = uniformGrid[:, 0]
        # print(x)
        # y = uniformGrid[:, 1]
        # z = uniformGrid[:, 2]
        kCellSize = 0.014408772790049425*3.    
        t = 0.5  # the isovalue, change if you want
        gyroid = (torch.cos(2*3.14*x[:, 0]/kCellSize) * torch.sin(2*3.14*x[:, 1]/kCellSize) + \
                  torch.cos(2*3.14*x[:, 1]/kCellSize) * torch.sin(2*3.14*x[:, 2]/kCellSize) + \
                  torch.cos(2*3.14*x[:, 2]/kCellSize) * torch.sin(2*3.14*x[:, 0]/kCellSize)) - t**2
        gyroid = torch.tensor(gyroid, device='cuda:0', dtype=torch.float32)
        gyroid = gyroid.reshape(-1, 1)
        # return self.sdf_net(x)
        # return gyroid
        return torch.max(gyroid, self.sdf_net(x))
        

if __name__ == '__main__':

    # Parse
    parser = parse_options(return_parser=True)
    app_group = parser.add_argument_group('app')
    app_group.add_argument('--img-dir', type=str, default='_results/render_app/imgs',
                           help='Directory to output the rendered images')
    app_group.add_argument('--render-2d', action='store_true',
                           help='Render in 2D instead of 3D')
    app_group.add_argument('--exr', action='store_true',
                           help='Write to EXR')
    app_group.add_argument('--r360', action='store_true',
                           help='Render a sequence of spinning images.')
    app_group.add_argument('--rsphere', action='store_true',
                           help='Render around a sphere.')
    app_group.add_argument('--sdf_grid', action='store_true',
                           help='Creates a uniform grid of with x samples per dimension'
                           ' and evalulates sdf at each point, and dumps the data in ./sdf.csv')
    app_group.add_argument('--nb-poses', type=int, default=64,
                           help='Number of poses to render for sphere rendering.')
    app_group.add_argument('--cam-radius', type=float, default=4.0,
                           help='Camera radius to use for sphere rendering.')
    app_group.add_argument('--disable-aa', action='store_true',
                           help='Disable anti aliasing.')
    app_group.add_argument('--export', type=str, default=None,
                           help='Export model to C++ compatible format.')
    app_group.add_argument('--rotate', type=float, default=None,
                           help='Rotation in degrees.')
    app_group.add_argument('--depth', type=float, default=0.0,
                           help='Depth of 2D slice.')
    args = parser.parse_args()

    # Pick device
    use_cuda = torch.cuda.is_available()
    device = torch.device('cuda' if use_cuda else 'cpu')

    # Get model name
    if args.pretrained is not None:
        name = args.pretrained.split('/')[-1].split('.')[0]
    else:
        assert False and "No network weights specified!"

    org_net = globals()[args.net](args)
    if args.jit:
        org_net = torch.jit.script(org_net)
    
    org_net.load_state_dict(torch.load(args.pretrained))

    org_net.to(device)
    org_net.eval()
    net = GyroidLattice(org_net)
    net.to(device)
    net.eval()
    
    print("Total number of parameters: {}".format(sum(p.numel() for p in net.parameters())))
    
    if args.export is not None:
        net = SOL_NGLOD(net)
       
        net.save(args.export)
        sys.exit()

    if args.sol:
        net = SOL_NGLOD(net)

    if args.lod is not None:
        net.lod = args.lod

    # Make output directory
    ins_dir = os.path.join(args.img_dir, name)
    if not os.path.exists(ins_dir):
        os.makedirs(ins_dir)

    for t in ['normal', 'rgb', 'exr']:
        _dir = os.path.join(ins_dir, t)
        if not os.path.exists(_dir):
            os.makedirs(_dir)

    renderer = Renderer(args, device, net).eval()

    if args.rotate is not None:
        rad = np.radians(args.rotate)
        model_matrix = torch.FloatTensor(R.from_rotvec(rad * np.array([0,1,0])).as_matrix())
    else:
        model_matrix = torch.eye(3)

    if args.r360:
        for angle in np.arange(0, 360, 10):
            rad = np.radians(angle)
            model_matrix = torch.FloatTensor(R.from_rotvec(rad * np.array([-1./np.sqrt(3.),-1./np.sqrt(3.),-1./np.sqrt(3.)])).as_matrix())
            
            out = renderer.shade_images(f=args.camera_origin,
                                        t=args.camera_lookat,
                                        fv=args.camera_fov,
                                        aa=not args.disable_aa,
                                        mm=model_matrix)
            
            # data = out.float().numpy().exrdict()
            
            idx = int(math.floor(100 * angle))

            # if args.exr:
            #     write_exr('{}/exr/{:06d}.exr'.format(ins_dir, idx), data)
            
            img_out = out.image().byte().numpy()
            Image.fromarray(img_out.rgb).save('{}/rgb/{:06d}.png'.format(ins_dir, idx), mode='RGB')
            # Image.fromarray(img_out.normal).save('{}/normal/{:06d}.png'.format(ins_dir, idx), mode='RGB')
    
    elif args.rsphere:
        views = sample_fib_sphere(args.nb_poses)
        cam_origins = args.cam_radius * views
        for p, cam_origin in enumerate(cam_origins):
            out = renderer.shade_images(f=cam_origin,
                                        t=args.camera_lookat,
                                        fv=args.camera_fov,
                                        aa=not args.disable_aa,
                                        mm=model_matrix)
            
            data = out.float().numpy().exrdict()
            
            if args.exr:
                write_exr('{}/exr/{:06d}.exr'.format(ins_dir, p), data)
            
            img_out = out.image().byte().numpy()
            # Image.fromarray(img_out.rgb).save('{}/rgb/{:06d}.png'.format(ins_dir, p), mode='RGB')
            Image.fromarray(img_out.normal).save('{}/normal/{:06d}.png'.format(ins_dir, p), mode='RGB')
    
    elif args.sdf_grid:
        # Create a uniform grid on torch. 
        # x range [-1.1, 1.1], y range [-1.1, 1.1], z range [-1.1 to 1.1]
        K = np.linspace(-1.1, 1.1, 150)
        grid = [[x,y,z] for x in K for y in K for z in K]
        torch_grid = torch.tensor(np.array(grid), device='cuda:0')
        print("shape of torch grid: ", torch_grid.size())
        # print(torch_grid)
        net.eval()
        sdf = net(torch_grid)
        print("shape of sdf grid: ", sdf.size())
        print(sdf)
        # Compute SDF on the torch_grid to torch_sdf
        r = np.hstack(sdf.detach().cpu().numpy(), torch_grid.detach().cpu().numpy())
        np.savetxt("sdf.csv", r, delimiter=",")
        exit(1)

    else:
        print("[INFO] here it is")
        # out = renderer.shade_images(f=args.camera_origin, 
        #                             t=args.camera_lookat, 
        #                             fv=args.camera_fov, 
        #                             aa=not args.disable_aa, 
        #                             mm=model_matrix)
        
        # data = out.float().numpy().exrdict()
        
        # if args.render_2d:
        # depth = args.depth
        print("[INFO] sdf slice")
        # data['sdf_slice'] = renderer.sdf_slice(depth=depth)
        # renderer.sdf_slice(depth=depth)
        Kx = np.linspace(-1.,1., 150)
        Ky = np.linspace(-0.3, 0.5, 150)
        Kz = np.linspace(-0.3, 0.8, 150)
        grid = [[x,y,z] for x in Kx for y in Ky for z in Kz]
        torch_grid = torch.tensor(np.array(grid), device='cuda:0', dtype=torch.float32)
        with torch.no_grad():            
            sdf = net(torch_grid.reshape(-1, 3))

        np_sdf = sdf.detach().cpu().numpy()
        np_grid = torch_grid.detach().cpu().numpy()
        del sdf, torch_grid  
        cubeMarcher = CubeMarcher()
        cubeMarcher.march(np_grid, np_sdf)
        marchedMesh = cubeMarcher.getMesh()
        marchedMesh.save("./marched_gyroid.obj")
            # data['rgb_slice'] = renderer.rgb_slice(depth=depth)
            # data['normal_slice'] = renderer.normal_slice(depth=depth)
        
        # if args.exr:
        #     write_exr(f'{ins_dir}/out.exr', data)

        # img_out = out.image().byte().numpy()
        
        # Image.fromarray(img_out.rgb).save('{}/{}_rgb.png'.format(ins_dir, name), mode='RGB')
        # Image.fromarray(img_out.depth).save('{}/{}_depth.png'.format(ins_dir, name), mode='RGB')
        # Image.fromarray(img_out.normal).save('{}/{}_normal.png'.format(ins_dir, name), mode='RGB')
        # Image.fromarray(img_out.hit).save('{}/{}_hit.png'.format(ins_dir, name), mode='L')

