# Copyright (c) Facebook, Inc. and its affiliates. 
#   
# This source code is licensed under the MIT license found in the 
# LICENSE file in the root directory of this source tree.
import os
import time
import shutil
import uuid
import pytest
import ctypes
import torch
import bitsandbytes as bnb
import bitsandbytes.functional as F

from os.path import join
from itertools import product

import apex

def get_temp_dir():
    path = '/tmp/autoswap/{0}'.format(str(uuid.uuid4()))
    os.makedirs(path, exist_ok=True)
    return path

def rm_path(path):
    shutil.rmtree(path)

str2optimizers = {}
str2optimizers['adam_pytorch'] = (None, torch.optim.Adam, bnb.optim.Adam)
str2optimizers['adam_apex'] = (None, apex.optimizers.FusedAdam, bnb.optim.Adam)
str2optimizers['momentum_apex'] = (None, lambda pxx: apex.optimizers.FusedSGD(pxx, 0.01, 0.9), bnb.optim.Adam)
str2optimizers['momentum_pytorch'] = (None, lambda pxx: torch.optim.SGD(pxx, 0.01, 0.9), bnb.optim.Adam)
str2optimizers['lamb_apex'] = (None, lambda pxx: apex.optimizers.FusedLAMB(pxx, weight_decay=0.00, use_nvlamb=True), bnb.optim.Adam)
str2optimizers['lars_apex'] = (None, lambda pxx: apex.parallel.LARC.LARC(apex.optimizers.FusedSGD(pxx, 0.01, 0.9)), bnb.optim.Adam)

str2optimizers['adam'] = (torch.optim.Adam, bnb.optim.Adam)
str2optimizers['fused_adam'] = (apex.optimizers.FusedAdam, bnb.optim.Adam)
str2optimizers['momentum'] = (lambda pxx: torch.optim.SGD(pxx, 0.01, 0.9), lambda pxx: bnb.optim.SGD(pxx, 0.01, 0.9, block_wise=False))
str2optimizers['lars'] = (lambda pxx: bnb.optim.PytorchLARS(pxx, 0.01, 0.9), lambda pxx: bnb.optim.LARS(pxx, 0.01, 0.9))
str2optimizers['lamb'] = (lambda pxx: apex.optimizers.FusedLAMB(pxx, weight_decay=0.0, max_grad_norm=10000.0, eps=1e-8, use_nvlamb=True), bnb.optim.LAMB)
str2optimizers['rmsprop'] = (lambda pxx: torch.optim.RMSprop(pxx, 0.01, 0.9), lambda pxx: bnb.optim.RMSprop(pxx, 0.01, 0.9, block_wise=False))
str2optimizers['adam8bit'] = (torch.optim.Adam, lambda pxx: bnb.optim.Adam8bit(pxx, block_wise=False))
str2optimizers['momentum8bit'] = (lambda pxx: torch.optim.SGD(pxx, 0.01, 0.9), lambda pxx: bnb.optim.SGD8bit(pxx, 0.01, 0.9, block_wise=False))
str2optimizers['rmsprop8bit'] = (lambda pxx: torch.optim.RMSprop(pxx, 0.01, 0.9), lambda pxx: bnb.optim.RMSprop8bit(pxx, 0.01, 0.9, block_wise=False))
str2optimizers['lamb8bit'] = (lambda pxx: apex.optimizers.FusedLAMB(pxx, weight_decay=0.0, max_grad_norm=10000.0, eps=1e-8, use_nvlamb=True), bnb.optim.LAMB8bit)
str2optimizers['lars8bit'] = (lambda pxx: bnb.optim.PytorchLARS(pxx, 0.01, 0.9), lambda pxx: bnb.optim.LARS8bit(pxx, 0.01, 0.9))

str2optimizers['adam8bit_blockwise'] = (torch.optim.Adam, lambda pxx: bnb.optim.Adam8bit(pxx, block_wise=True))
str2optimizers['momentum8bit_blockwise'] = (lambda pxx: torch.optim.SGD(pxx, 0.01, 0.9), lambda pxx: bnb.optim.SGD8bit(pxx, 0.01, 0.9, block_wise=True))
str2optimizers['rmsprop8bit_blockwise'] = (lambda pxx: torch.optim.RMSprop(pxx, 0.01, 0.9), lambda pxx: bnb.optim.RMSprop8bit(pxx, 0.01, 0.9, block_wise=True))

str2statenames = {}
str2statenames['adam'] = [('exp_avg', 'state1'), ('exp_avg_sq', 'state2')]
str2statenames['momentum'] = [('momentum_buffer', 'state1')]
str2statenames['lars'] = [('momentum_buffer', 'state1')]
str2statenames['lamb'] = [('exp_avg', 'state1'), ('exp_avg_sq', 'state2')]
str2statenames['rmsprop'] = [('square_avg', 'state1')]
str2statenames['adam8bit'] = [('exp_avg', 'state1', 'qmap1', 'max1'), ('exp_avg_sq', 'state2', 'qmap2', 'max2')]
str2statenames['lamb8bit'] = [('exp_avg', 'state1', 'qmap1', 'max1'), ('exp_avg_sq', 'state2', 'qmap2', 'max2')]
str2statenames['adam8bit_blockwise'] = [('exp_avg', 'state1', 'qmap1', 'absmax1'), ('exp_avg_sq', 'state2', 'qmap2', 'absmax2')]
str2statenames['momentum8bit'] = [('momentum_buffer', 'state1', 'qmap1', 'max1')]
str2statenames['momentum8bit_blockwise'] = [('momentum_buffer', 'state1', 'qmap1', 'absmax1')]
str2statenames['lars8bit'] = [('momentum_buffer', 'state1', 'qmap1', 'max1')]
str2statenames['rmsprop8bit'] = [('square_avg', 'state1', 'qmap1', 'max1')]
str2statenames['rmsprop8bit_blockwise'] = [('square_avg', 'state1', 'qmap1', 'absmax1')]

dim1 = [1024]
dim2 = [32, 1024, 4097, 1]
gtype = [torch.float32, torch.float16]
optimizer_names = ['adam', 'momentum', 'rmsprop', 'lars', 'lamb']
values = list(product(dim1,dim2, gtype, optimizer_names))
names = ['dim1_{0}_dim2_{1}_gtype_{2}_optim_{3}'.format(*vals) for vals in values]
@pytest.mark.parametrize("dim1, dim2, gtype, optim_name", values, ids=names)
def test_optimizer32bit(dim1, dim2, gtype, optim_name):
    if dim1 == 1 and dim2 == 1: return
    p1 = torch.randn(dim1,dim2, device='cuda', dtype=gtype)*0.1
    p2 = p1.clone()
    p1 = p1.float()


    torch_optimizer = str2optimizers[optim_name][0]([p1])
    bnb_optimizer = str2optimizers[optim_name][1]([p2])

    if gtype == torch.float32:
        atol, rtol = 1e-6, 1e-5
    else:
        atol, rtol = 1e-4, 1e-3


    for i in range(50):
        g = torch.randn(dim1,dim2, device='cuda', dtype=gtype)*0.01
        p1.grad = g.clone().float()
        p2.grad = g.clone()

        bnb_optimizer.step()
        torch_optimizer.step()

        for name1, name2 in str2statenames[optim_name]:
            torch.testing.assert_allclose(torch_optimizer.state[p1][name1], bnb_optimizer.state[p2][name2], atol=atol, rtol=rtol)

        torch.testing.assert_allclose(p1, p2.float(), atol=atol, rtol=rtol)

        if i % 10 == 0 and i > 0:
            path = get_temp_dir()
            torch.save(bnb_optimizer.state_dict(),join(path, 'opt.pt'))
            del bnb_optimizer
            bnb_optimizer = None
            bnb_optimizer = str2optimizers[optim_name][1]([p2])
            bnb_optimizer.load_state_dict(torch.load(join(path, 'opt.pt')))
            rm_path(path)
            torch.testing.assert_allclose(p1, p2.float(), atol=atol, rtol=rtol)
            for name1, name2 in str2statenames[optim_name]:
                torch.testing.assert_allclose(torch_optimizer.state[p1][name1], bnb_optimizer.state[p2][name2], atol=atol, rtol=rtol)

        if gtype == torch.float16:
            # the adam buffers should also be close because they are 32-bit
            # but the paramters can diverge because they are 16-bit
            # the difference grow larger and larger with each update
            # --> copy the state to keep weights close
            p1.data = p1.data.half().float()
            p2.copy_(p1.data)
            torch.testing.assert_allclose(p1.half(), p2)
        if optim_name in ['lars', 'lamb']:
            assert bnb_optimizer.state[p2]['unorm_vec'] > 0.0

dim1 = [1024]
dim2 = [32, 1024, 4097]
gtype = [torch.float32, torch.float16]
values = list(product(dim1,dim2, gtype))
names = ['dim1_{0}_dim2_{1}_gtype_{2}'.format(*vals) for vals in values]
@pytest.mark.parametrize("dim1, dim2, gtype", values, ids=names)
def test_global_config(dim1, dim2, gtype):
    if dim1 == 1 and dim2 == 1: return
    p1 = torch.randn(dim1,dim2, device='cpu', dtype=gtype)*0.1
    p2 = torch.randn(dim1,dim2, device='cpu', dtype=gtype)*0.1
    p3 = torch.randn(dim1,dim2, device='cpu', dtype=gtype)*0.1
    mask = torch.rand_like(p2) < 0.1
    beta1 = 0.9
    beta2 = 0.999
    lr = 0.001
    eps = 1e-8

    bnb.optim.GlobalOptimManager.get_instance().initialize()
    bnb.optim.GlobalOptimManager.get_instance().override_config(p2, 'skip_zeros', True)
    bnb.optim.GlobalOptimManager.get_instance().override_config(p3, 'optim_bits', 8)

    bnb.optim.GlobalOptimManager.get_instance().register_parameters([p1, p2, p3])
    p1 = p1.cuda()
    p2 = p2.cuda()
    p3 = p3.cuda()

    adam2 = bnb.optim.Adam([p1, p2, p3], lr, (beta1, beta2), eps)

    if gtype == torch.float32:
        atol, rtol = 1e-6, 1e-5
    else:
        atol, rtol = 1e-4, 1e-3

    original_p2 = p2[mask].clone()

    for i in range(50):
        g1 = torch.randn(dim1,dim2, device='cuda', dtype=gtype)*0.1 + 0.001
        g2 = torch.randn(dim1,dim2, device='cuda', dtype=gtype)*0.1 + 0.001
        g3 = torch.randn(dim1,dim2, device='cuda', dtype=gtype)*0.1 + 0.001
        p1.grad = g1
        p2.grad = g2
        p3.grad = g3

        if i > 30 and i % 10 == 0:
            g1.data[mask] = 0.0
            g2.data[mask] = 0.0
            p1.grad = g1
            p2.grad = g2
            original_p1 = p1[mask].clone()
            original_p2 = p2[mask].clone()
            og_s1 = adam2.state[p2]['state1'][mask].clone()
            og_s2 = adam2.state[p2]['state2'][mask].clone()
            og_s11 = adam2.state[p1]['state1'][mask].clone()
            og_s21 = adam2.state[p1]['state2'][mask].clone()

        adam2.step()

        assert adam2.state[p3]['state1'].dtype == torch.uint8
        assert adam2.state[p3]['state2'].dtype == torch.uint8

        if i > 30 and i % 10 == 0:
            torch.testing.assert_allclose(original_p2, p2[mask])
            torch.testing.assert_allclose(adam2.state[p2]['state1'][mask], og_s1)
            torch.testing.assert_allclose(adam2.state[p2]['state2'][mask], og_s2)
            assert ((p1[mask]- original_p1)==0.0).sum() < p1.numel()
            assert ((adam2.state[p1]['state1'][mask]- og_s11)==0.0).sum() == 0.0
            assert ((adam2.state[p1]['state2'][mask]- og_s21)==0.0).sum() == 0.0




dim1 = [1024]
dim2 = [32, 1024, 4097]
gtype = [torch.float32, torch.float16]
optimizer_names = ['adam8bit', 'momentum8bit', 'rmsprop8bit', 'adam8bit_blockwise', 'lamb8bit', 'lars8bit', 'momentum8bit_blockwise', 'rmsprop8bit_blockwise']
values = list(product(dim1,dim2, gtype, optimizer_names))
names = ['dim1_{0}_dim2_{1}_gtype_{2}_optim_{3}'.format(*vals) for vals in values]
@pytest.mark.parametrize("dim1, dim2, gtype, optim_name", values, ids=names)
def test_optimizer8bit(dim1, dim2, gtype, optim_name):
    if dim1 == 1 and dim2 == 1: return
    p1 = torch.randn(dim1,dim2, device='cuda', dtype=gtype)*0.1
    p2 = p1.clone()
    p1 = p1.float()
    blocksize = 2048

    torch_optimizer = str2optimizers[optim_name][0]([p1])
    bnb_optimizer = str2optimizers[optim_name][1]([p2])

    if gtype == torch.float32:
        atol, rtol = 3e-3, 1e-3
        patol, prtol = 1e-5, 1e-3

    else:
        atol, rtol = 3e-3, 1e-3
        patol, prtol = 1e-5, 1e-3

    errors = []
    relerrors = []

    for i in range(50):
        g = torch.randn(dim1,dim2, device='cuda', dtype=gtype)*0.01
        p1.grad = g.clone().float()
        p2.grad = g.clone()

        bnb_optimizer.step()
        torch_optimizer.step()

        torch.testing.assert_allclose(p1, p2.float(), atol=patol, rtol=prtol)

        dequant_states = []
        for name1, name2, qmap, max_val in str2statenames[optim_name]:
            #print(bnb_optimizer.state[p2][max_val], name1)
            if 'blockwise' in optim_name:
                s1 = F.dequantize_blockwise(code=bnb_optimizer.state[p2][qmap], absmax=bnb_optimizer.state[p2][max_val], A=bnb_optimizer.state[p2][name2], blocksize=blocksize)
            else:
                s1 = F.dequantize(code=bnb_optimizer.state[p2][qmap], absmax=bnb_optimizer.state[p2][max_val], A=bnb_optimizer.state[p2][name2])
            num_not_close = torch.isclose(torch_optimizer.state[p1][name1], s1, atol=atol, rtol=rtol)==0
            assert num_not_close.sum().item() < 20
            dequant_states.append(s1.clone())

        err  = torch.abs(p1-p2)
        relerr = err/torch.abs(p1)
        assert err.mean() < 0.0001
        assert relerr.mean() < 0.001

        errors.append(err.mean().item())
        relerrors.append(relerr.mean().item())

        if i % 10 == 0 and i > 0:
            for (name1, name2, qmap, max_val), s in zip(str2statenames[optim_name], dequant_states):
                s1cpy = s.clone()
                raws1cpy = bnb_optimizer.state[p2][name2].clone()
                qmap1 = bnb_optimizer.state[p2][qmap].clone()

                path = get_temp_dir()
                torch.save(bnb_optimizer.state_dict(),join(path, 'opt.pt'))
                del bnb_optimizer
                bnb_optimizer = None
                bnb_optimizer = str2optimizers[optim_name][1]([p2])
                bnb_optimizer.load_state_dict(torch.load(join(path, 'opt.pt')))
                rm_path(path)
                torch.testing.assert_allclose(raws1cpy, bnb_optimizer.state[p2][name2])
                torch.testing.assert_allclose(qmap1, bnb_optimizer.state[p2][qmap])

                if 'blockwise' in optim_name:
                    s1 = F.dequantize_blockwise(code=bnb_optimizer.state[p2][qmap], absmax=bnb_optimizer.state[p2][max_val], A=bnb_optimizer.state[p2][name2], blocksize=blocksize)
                else:
                    s1 = F.dequantize(code=bnb_optimizer.state[p2][qmap], absmax=bnb_optimizer.state[p2][max_val], A=bnb_optimizer.state[p2][name2])
                torch.testing.assert_allclose(s1cpy, s1)

                num_not_close = torch.isclose(torch_optimizer.state[p1][name1], s1, atol=atol, rtol=rtol)==0
                assert num_not_close.sum().item() < 20
            torch.testing.assert_allclose(p1, p2.float(), atol=patol, rtol=prtol)

        # the parameters diverge quickly. Here we keep them close
        # together so we can test against the Adam error
        p1.data = p1.data.to(gtype).float()
        p2.copy_(p1.data)
        torch.testing.assert_allclose(p1.to(gtype), p2)
        for (name1, name2, qmap, max_val), s in zip(str2statenames[optim_name], dequant_states):
            torch_optimizer.state[p1][name1].copy_(s.data)

    #print(sum(errors)/len(errors))
    #print(sum(relerrors)/len(relerrors))



dim1 = [1024]
dim2 = [32, 1024, 4097]
gtype = [torch.float32]
optim_bits = [32, 8]
values = list(product(dim1,dim2, gtype, optim_bits))
names = ['dim1_{0}_dim2_{1}_gtype_{2}_optim_bits_{3}'.format(*vals) for vals in values]
@pytest.mark.parametrize("dim1, dim2, gtype, optim_bits", values, ids=names)
def test_adam_percentile_clipping(dim1, dim2, gtype, optim_bits):
    if dim1 == 1 and dim2 == 1: return
    p1 = torch.randn(dim1,dim2, device='cpu', dtype=gtype)*0.1
    beta1 = 0.9
    beta2 = 0.999
    lr = 0.001
    eps = 1e-8
    p1 = p1.cuda()
    p2 = p1.clone()
    adam1 = bnb.optim.Adam([p1], lr, (beta1, beta2), eps, optim_bits=optim_bits)
    adam2 = bnb.optim.Adam([p2], lr, (beta1, beta2), eps, optim_bits=optim_bits, percentile_clipping=5)

    gnorm_vec = torch.zeros(100).cuda()
    step = 0

    for i in range(50):
        step += 1
        g1 = torch.randn(dim1,dim2, device='cuda', dtype=gtype)*0.1 + (0.01*i)
        g2 = g1.clone()
        p2.grad = g2

        current_gnorm, clip_val, gnorm_scale = F.percentile_clipping(g1, gnorm_vec, step, 5)
        g1 = (g1.float()*gnorm_scale).to(gtype)
        p1.grad = g1

        adam1.step()
        adam2.step()

        # gnorm_scale is not deterministic (warp reductions), as such there can be slight differences in state
        if optim_bits == 32:
            torch.testing.assert_allclose(p1, p2)
            torch.testing.assert_allclose(adam1.state[p1]['state1'], adam2.state[p2]['state1'], atol=5e-5, rtol=1e-4)
            torch.testing.assert_allclose(adam1.state[p1]['state2'], adam2.state[p2]['state2'], atol=5e-5, rtol=1e-4)
        elif optim_bits == 8:
            torch.testing.assert_allclose(p1, p2, atol=1e-4, rtol=1e-3)
            torch.testing.assert_allclose(adam1.state[p1]['state1'], adam2.state[p2]['state1'], atol=2, rtol=1e-3)
            torch.testing.assert_allclose(adam1.state[p1]['state2'], adam2.state[p2]['state2'], atol=2, rtol=1e-3)
            adam1.state[p1]['state1'].copy_(adam2.state[p2]['state1'])
            adam1.state[p1]['state2'].copy_(adam2.state[p2]['state2'])
        if i % 10 == 0 and i > 0:
            path = get_temp_dir()
            torch.save(adam2.state_dict(),join(path, 'opt.pt'))
            del adam2
            adam2 = None
            adam2 = bnb.optim.Adam([p2], lr, (beta1, beta2), eps, optim_bits=optim_bits, percentile_clipping=5)
            adam2.load_state_dict(torch.load(join(path, 'opt.pt')))




dim1 = [4096]
dim2 = [4096]
gtype = [torch.float32, torch.float16]
#optimizer_names = ['adam8bit_blockwise', 'adam8bit', 'lamb8bit']
#optimizer_names = ['adam8bit_blockwise', 'adam_apex', 'adam8bit', 'adam', 'adam_pytorch']
#optimizer_names = ['momentum_apex', 'momentum8bit', 'momentum_pytorch']
#optimizer_names = ['lamb_apex', 'lamb8bit']
#optimizer_names = ['lars_apex', 'lars8bit']
optimizer_names = ['adam8bit_blockwise']
values = list(product(dim1,dim2, gtype, optimizer_names))
names = ['dim1_{0}_dim2_{1}_gtype_{2}_optim_{3}'.format(*vals) for vals in values]
@pytest.mark.parametrize("dim1, dim2, gtype, optim_name", values, ids=names)
def test_benchmark_blockwise(dim1, dim2, gtype, optim_name):
    if dim1 == 1 and dim2 == 1: return
    p1 = torch.randn(dim1,dim2, device='cuda', dtype=gtype)*0.1


    bnb_optimizer = str2optimizers[optim_name][1]([p1])

    g = torch.randn(dim1,dim2, device='cuda', dtype=gtype)*0.01
    p1.grad = g
    for i in range(5000):
        if i == 500:
            # 100 iterations for burn-in
            torch.cuda.synchronize()
            t0 = time.time()

        bnb_optimizer.step()

    torch.cuda.synchronize()
    s = time.time()-t0
    print('')
    params = 4500*4096*4096
    print(optim_name, gtype, s/params)
    #assert s < 3.9


