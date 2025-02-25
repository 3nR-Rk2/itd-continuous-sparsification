import sys
import os
import time
import datetime
import argparse
import shutil
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torch.backends.cudnn as cudnn
import numpy as np
import random
from models import *
import torch.distributed as dist
import torch.utils.data.distributed
from load_datasets import *

#from torch.utils.tensorboard import SummaryWriter

parser = argparse.ArgumentParser(description='Training a ResNet on ImageNet or CIFAR-10 with Continuous Sparsification')
parser.add_argument('--dataset', type=str, default='cifar10', help='which dataset to use(cifar10 or ImageNet)')
parser.add_argument('--input-dir', type=str, default='./', help='input directory if resume is True')
parser.add_argument('--output-dir', type=str, default='./', help='output directory')
parser.add_argument('--resume', type=bool, default=False, help='whether use saving model or not')
parser.add_argument('--model-path', type=str, default='/checkpoint.pt', help='model path under an output directory')
parser.add_argument('--start-epoch', type=int, default='3', help='start epoch')
parser.add_argument('--world-size', type=int, default=1, help='world_size')
parser.add_argument('--rank',type=int, default=1, help='node rank for distributed training')
parser.add_argument('--distributed', type=bool, default=False,help='use distributed training or not')
parser.add_argument('--dist-backend', default='nccl', type=str, help='distributed backend')
parser.add_argument('--dist-url', default='tcp://172.17.0.9:39999', type=str, help='url used to set up distributed training')
parser.add_argument('--which-gpu', type=int, default=0, help='which GPU to use')
parser.add_argument('--num-classes', type=int, help='number of classes')
parser.add_argument('--batch-size', type=int, default=64, metavar='N', help='input batch size for training/val/test (default: 128)')
parser.add_argument('--epochs', type=int, default=90, help='number of epochs to train (default: 85)')
parser.add_argument('--rounds', type=int, default=1, help='number of rounds to train (default: 3)')
parser.add_argument('--lr', type=float, default=0.1, metavar='LR', help='learning rate (default: 0.1)')
parser.add_argument('--no-cuda', action='store_true', default=False, help='disables CUDA training')
parser.add_argument('--seed', type=int, default=1234, metavar='S', help='random seed (default: 1234)')
parser.add_argument('--workers', type=int, default=4, help='number of data loading workers (default: 2)')
parser.add_argument('--val-set-size', type=int, default=5000, help='how much of the training set to use for validation (default: 5000)')
parser.add_argument('--lr-schedule', type=int, nargs='+', default=[30,60], help='epochs at which the learning rate will be dropped')
parser.add_argument('--lr-drops', type=float, nargs='+', default=[0.1, 0.1], help='how much to drop the lr at each epoch in the schedule')
parser.add_argument('--decay', type=float, default=0.0001, help='weight decay (default: 0.0001)')
parser.add_argument('--rewind-epoch', type=int, default=2, help='epoch to rewind weights to (default: 2)')
parser.add_argument('--lmbda', type=float, default=1e-8, help='lambda for L1 mask regularization (default: 1e-8)')
parser.add_argument('--final-temp', type=float, default=200, help='temperature at the end of each round (default: 200)')
parser.add_argument('--mask-initial-value', type=float, default=-0.01, help='initial value for mask parameters')

args = parser.parse_args()

#run_time + times.times()
#wr_path = args.output_dir + '/runs/' + str(run_time)
#writer = SummaryWriter(wr_path)

args.cuda = not args.no_cuda and torch.cuda.is_available()
num_devices = torch.cuda.device_count()
if args.world_size > num_devices:
    print('number of world size is more than number of available GPU!!')
    sys.exit()
print('number of devices is {}'.format(num_devices))
torch.manual_seed(args.seed)
np.random.seed(args.seed)
random.seed(args.seed)

if args.cuda:
    torch.cuda.manual_seed_all(args.seed)
    cudnn.benchmark = True
print('seed is set.')

if args.distributed:
    dist.init_process_group(backend=args.dist_backend, init_method=args.dist_url,world_size=args.world_size, rank=args.rank)
print('init_process group is set.')

if args.dataset == 'cifar10':
    train_loader, val_loader, test_loader = generate_loaders(args.val_set_size, args.batch_size, args.workers)
elif args.dataset == 'ImageNet':
    train_loader, val_loader = ImageNet_generate_loaders(args.batch_size, args.workers, args.distributed)
else:
    print('dataset is not available on this program!!')
    sys.exit()

# num_class=1000 if dataset is ImageNet, num_classes=10 if dataset is cifar10.
model = ResNet50(args.num_classes, args.mask_initial_value)

#images, labels = next(iter(train_loader))
#writer.add_graph(model, images)
#writer.close()

if not args.cuda:
    print('using CPU, this will be slow')
elif args.distributed:
    if args.which_gpu is not None:
        torch.cuda.set_device(args.which_gpu)
        model.cuda(args.which_gpu)
        args.batch_size = int(args.batch_size/args.world_size)
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.which_gpu])
    else:
        model.cuda()
        # DistributedDataParallel will divide and allocate batch_size to all
        # available GPUs if device_ids are not set
        model = torch.nn.parallel.DistributedDataParallel(model)
elif args.which_gpu is not None:
    torch.cuda.set_device(args.which_gpu)
    model = model.cuda(args.which_gpu)
else:
    model = model.cuda()

print(model)

def adjust_learning_rate(optimizer, epoch):
    lr = args.lr
    assert len(args.lr_schedule) == len(args.lr_drops), "length of gammas and schedule should be equal"
    for (drop, step) in zip(args.lr_drops, args.lr_schedule):
        if (epoch >= step): lr = lr * drop
        else: break
    for param_group in optimizer.param_groups: param_group['lr'] = lr

def compute_remaining_weights(masks):
    return 1 - sum(float((m == 0).sum()) for m in masks) / sum(m.numel() for m in masks)

def train(outer_round, best_acc, epochs, start_epoch = 0, output_dir=args.output_dir,filename='/checkpoint.pt'):
    running_loss = 0.0
    running_correct = 0
    for epoch in range(start_epoch, epochs):
        print('\t--------- Epoch {} -----------'.format(epoch))
        start = time.time()
        model.train()
        if epoch > 0: model.temp *= temp_increase  
        if outer_round == 0 and epoch == args.rewind_epoch:
            model.checkpoint()
            filename_rewind = output_dir + '/checkpoint_rewind.pt' 
            save_checkpoint({
                'epoch': epoch + 1,
                'arch': 'RestNet50',
                'state_dict': model.state_dict(),
                'best_acc1': best_acc,
                'weight_optim': optimizers[0].state_dict(),
                'mask_optim': optimizers[1].state_dict()
            },filename=filename_rewind)
        for optimizer in optimizers: adjust_learning_rate(optimizer, epoch)

        data_num = 0
        for batch_idx, (data, target) in enumerate(train_loader):
            data_num += data.size()[0]
            if args.cuda:
                if args.which_gpu is not None:
                    data, target = data.cuda(device=args.which_gpu), target.cuda(device=args.which_gpu, non_blocking=True)
                else:
                    data, target = data.cuda(), target.cuda(non_blocking=True)
            for optimizer in optimizers: optimizer.zero_grad()
            output = model(data)
            pred = output.max(1)[1]
            running_correct += (pred == target).sum().item()
            batch_correct = pred.eq(target.data.view_as(pred)).sum()
            masks = [m.mask for m in model.mask_modules]
            entries_sum = sum(m.sum() for m in masks)
            loss = F.cross_entropy(output, target) + args.lmbda * entries_sum
            running_loss += loss.item()
            loss.backward()
            for optimizer in optimizers: optimizer.step()
        train_time = time.time()
        train_pr = train_time - start
        val_acc = test(val_loader)
        val_time = time.time()
        val_pr = val_time - train_time
        #test_acc = test(test_loader)
        best_acc_name = output_dir + filename
        if val_acc > best_acc and filename='/checkpoint.pt':
            best_acc = val_acc
            save_checkpoint({
                'epoch': epoch + 1,
                'arch': 'RestNet50',
                'state_dict': model.state_dict(),
                'best_acc1': best_acc,
                'weight_optim': optimizers[0].state_dict(),
                'mask_optim': optimizers[1].state_dict()
            },filename=best_acc_name)
            save_time = time.time()
            save_pr = save_time - val_time
        else if filename = '/final_ticket_checkpoint.pt':
            best_acc = val_acc
            save_checkpoint({
                'epoch': epoch + 1,
                'arch': 'RestNet50',
                'state_dict': model.state_dict(),
                'best_acc1': best_acc,
                'weight_optim': optimizers[0].state_dict()
            })
        else: save_pr = 0.
        remaining_weights = compute_remaining_weights(masks)
        print('\t\tTemp: {:.1f}\tRemaining weights: {:.4f}\tVal acc: {:.1f}'.format(model.temp, remaining_weights, val_acc))
        print('\t\tTraining period: {:.1f}\tValidating period: {:.1f}\tSaving: {:.1f}'.format(train_pr, val_pr, save_pr))
        print('\t\tTraining loss: {:.1f}\tTraining accuracy: {:.1f}'.format(100*running_loss/data_num, 100*running_correct/data_num))
        #writer.add_scalar('training loss', running_loss / data_num, epoch + 1)
        #writer.add_scalar('training accuracy', running_correct / data_num, epoch + 1)
        #writer.add_scalar('validation accuracy', val_acc, epoch + 1)
        #writer.close()
        data_num = 0
        running_loss = 0.0
        running_correct = 0
    return best_acc
        
def test(loader):
    model.eval()
    correct = 0.
    total = 0.
    with torch.no_grad():
        for data, target in loader:
            if args.cuda:
                if args.which_gpu is not None:
                    data, target = data.cuda(device=args.which_gpu), target.cuda(device=args.which_gpu, non_blocking=True)
                else:
                    data, target = data.cuda(), target.cuda(non_blocking=True)
            output = model(data)
            pred = output.max(1)[1]
            correct += pred.eq(target.data.view_as(pred)).sum()
            total += data.size()[0]
    acc = 100. * correct.item() / total
    return acc

def save_checkpoint(state, filename='checkpoint.pt'):
    torch.save(state, filename)
    shutil.copyfile(filename, 'model_best.pt')

time_stump = datetime.datetime.now()
new_dir_path = args.output_dir + str(time_stump.date()) + str(time_stump.time())
os.makedirs(new_dir_path)

iters_per_reset = args.epochs-1
temp_increase = args.final_temp**(1./iters_per_reset)

if args.resume:
    input_file = args.input_dir + args.model_path 
    if not args.cuda:
        checkpoint = torch.load(input_file)
    else:
        loc = 'cuda:{}'.format(args.which_gpu)
        checkpoint = torch.load(input_file, map_location=loc)
    args.start_epoch = checkpoint['epoch'] - 1
    model.load_state_dict(checkpoint['state_dict'])
    # optimizers[0].load_state_dict(checkpoint['weight_optim'])
    # optimizers[1].load_state_dict(checkpoint['mask_optim'])
    print("=> loaded checkpoint '{}' (epoch {})".format(args.resume, checkpoint['epoch']))
else:
    print("=> no checkpoint found at '{}".format(args.resume))

trainable_params = filter(lambda p: p.requires_grad, model.parameters())
num_params = sum([p.numel() for p in trainable_params])
print("Total number of parameters: {}".format(num_params))

weight_params = map(lambda a: a[1], filter(lambda p: p[1].requires_grad and 'mask' not in p[0], model.named_parameters()))
mask_params = map(lambda a: a[1], filter(lambda p: p[1].requires_grad and 'mask' in p[0], model.named_parameters()))

model.ticket = False
weight_optim = optim.SGD(weight_params, lr=args.lr, momentum=0.9, nesterov=False, weight_decay=args.decay)
mask_optim = optim.SGD(mask_params, lr=args.lr, momentum=0.9, nesterov=False)
optimizers = [weight_optim, mask_optim]
best_acc = 0

for outer_round in range(args.rounds):
    print('--------- Round {} -----------'.format(outer_round))
    best_acc = train(outer_round, best_acc, args.epochs, args.start_epoch, output_dir=new_dir_path)
    model.temp = 1
    if outer_round != args.rounds-1: model.prune()
print('--------- Training final ticket -----------')
optimizers = [optim.SGD(weight_params, lr=args.lr, momentum=0.0, nesterov=False, weight_decay=args.decay)]
model.ticket = True
model.rewind_weights()
best_acc = 0
best_acc = train(args.rounds, best_acc, epochs=1, output_dir=new_dir_path, filename='/final_ticket_checkpoint.pt')
print('final best accuracy is {}'.format(best_acc))
