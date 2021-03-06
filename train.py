import torch

# base libraries
import argparse
import os
import setproctitle
import shutil
import csv

# internals
from src import *

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

default_train_images = os.path.join(BASE_DIR, 'data/train_images')
default_csv = os.path.join(BASE_DIR, 'data/train.csv')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-n', '--network-name', type=str, required=True)
    parser.add_argument('-d', '--dataset-name', type=str, required=True)
    parser.add_argument('-p', '--pretrained', type=bool, default=False)
    parser.add_argument('-dp', '--data-parallel', type=bool, default=True)
    parser.add_argument('--train-images-path', type=str, default=default_train_images)
    parser.add_argument('--train-csv-path', type=str, default=default_csv)
    parser.add_argument('-l', '--load')
    parser.add_argument('--batchSz', type=int, default=32) # 64
    parser.add_argument('--nEpochs', type=int, default=1) # 300
    parser.add_argument('--sEpoch', type=int, default=1)
    parser.add_argument('--nSubsample', type=int, default=0)
    parser.add_argument('--use-cuda', type=str, default='no')
    parser.add_argument('--nGPU', type=int, default=0)
    parser.add_argument('--save')
    parser.add_argument('--seed', type=int, default=50)
    parser.add_argument('--opt', type=str, default='sgd', choices=('sgd', 'adam', 'rmsprop'))
    parser.add_argument('--crit', type=str, default='bce', choices=('bce', 'f1'))
    args = parser.parse_args()

    if args.use_cuda == 'yes' and not torch.cuda.is_available():
        raise ValueError('Use cuda requires cuda devices and ' + \
                         'drivers to be installed. Please make ' + \
                         'sure both are installed.'
                        )
    elif args.use_cuda == 'yes' and torch.cuda.is_available():
        args.cuda = True
    else:
        args.cuda = False

    if args.cuda and args.nGPU == 0:
        nGPU = 1
    else:
        nGPU = args.nGPU

    print("using cuda ", args.cuda)

    args.save = args.save or 'work/%s/%s' % \
                                (args.network_name, args.dataset_name)
    setproctitle.setproctitle(args.save)

    torch.manual_seed(args.seed)
    if args.cuda:
        torch.cuda.manual_seed(args.seed)

    if os.path.exists(args.save):
        shutil.rmtree(args.save)
    os.makedirs(args.save, exist_ok=True)

    kwargs = {'batch_size': args.batchSz}

    trainLoader, devLoader = get_train_test_split(args, **kwargs)

    if args.load:
        print("Loading network: {}".format(args.load))
        net = torch.load(args.load)
    else:
        net = get_network(args.network_name, args.pretrained)

    if args.data_parallel:
        net = torch.nn.DataParallel(net)

    print('  + Number of params: {}'.format(sum([p.data.nelement() for p in net.parameters()])))

    if args.cuda:
        net = net.cuda()

    if args.opt == 'sgd':
        optimizer = torch.optim.SGD(net.parameters(), lr=1e-1, momentum=0.9, weight_decay=1e-4)
    elif args.opt == 'adam':
        optimizer = torch.optim.Adam(net.parameters(), weight_decay=1e-4)
    elif args.opt == 'rmsprop':
        optimizer = torch.optim.RMSprop(net.parameters(), weight_decay=1e-4)
    else:
        raise ModuleNotFoundError('optimiser not found')

    criterion = get_loss_function(args.crit)

    trainF = open(os.path.join(args.save, 'train.csv'), 'a')
    testF = open(os.path.join(args.save, 'test.csv'), 'a')

    for epoch in range(args.sEpoch, args.nEpochs + args.sEpoch):
        adjust_opt(args.opt, optimizer, epoch)
        unfreeze_weights(args.pretrained, net, epoch)
        train(args, epoch, net, trainLoader, criterion, optimizer, trainF)
        test(args, epoch, net, devLoader, criterion, optimizer, testF)
        torch.save(net, os.path.join(args.save, '%d.pth' % epoch))

    trainF.close()
    testF.close()

def train(args, epoch, net, trainLoader, criterion, optimizer, trainF):
    net.train()
    nProcessed = 0
    nTrain = len(trainLoader.dataset)
    for batch_idx, data in enumerate(trainLoader):
        inputs, labels = data['image'], data['labels']
        # get the inputs
        if args.cuda:
            inputs = inputs.cuda()
            labels = labels.cuda()

        # zero the parameter gradients
        optimizer.zero_grad()

        # forward + backward + optimize
        outputs = net(inputs)
        loss = criterion(outputs, labels)
        loss.backward()
        optimizer.step()
        nProcessed += len(data)
        pred = outputs.data.gt(0.5)
        tp = (pred + labels.data.byte()).eq(2).sum().float()
        fp = (pred - labels.data.byte()).eq(1).sum().float()
        fn = (pred - labels.data.byte()).eq(255).sum().float()
        tn = (pred + labels.data.byte()).eq(0).sum().float()
        acc = (tp + tn) / (tp + tn + fp + fn)
        try:
            prec = tp / (tp + fp)
        except ZeroDivisionError:
            prec = 0.0
        try:
            rec = tp / (tp + fn)
        except ZeroDivisionError:
            rec = 0.0
        partialEpoch = epoch + batch_idx / len(trainLoader) - 1
        print('Train Epoch: {:.2f} [{}/{} ({:.0f}%)]\tLoss: {:.4f}\tAcc: {:.4f}\tPrec: {:.4f}\tRec: {:.4f}\tTP: {}\tFP: {}\tFN: {}\tTN: {}'.format(
            partialEpoch, nProcessed, nTrain, 100. * batch_idx / len(trainLoader),
            loss.item(), acc, prec, rec, tp, fp, fn, tn))
        trainF.write('{},{},{},{},{}\n'.format(partialEpoch, loss.item(), acc, prec, rec))
        trainF.flush()

def test(args, epoch, net, devLoader, criterion, optimizer, testF):
    net.eval()
    test_loss = 0
    acc = prec = rec = 0
    incorrect = 0

    with torch.no_grad():
        for batch_idx, data in enumerate(devLoader):
            inputs, labels = data['image'], data['labels']
            if args.cuda:
                inputs = inputs.cuda()
                labels = labels.cuda()

            outputs = net(inputs)
            test_loss += criterion(outputs, labels)
            pred = outputs.data.gt(0.5)
            tp = (pred + labels.data.byte()).eq(2).sum().float()
            fp = (pred - labels.data.byte()).eq(1).sum().float()
            fn = (pred - labels.data.byte()).eq(255).sum().float()
            tn = (pred + labels.data.byte()).eq(0).sum().float()
            acc += (tp + tn) / (tp + tn + fp + fn)
            try:
                prec += tp / (tp + fp)
            except ZeroDivisionError:
                prec += 0.0
            try:
                rec += tp / (tp + fn)
            except ZeroDivisionError:
                rec += 0.0
        test_loss /= len(devLoader)
        acc /= len(devLoader)
        prec /= len(devLoader)
        rec /= len(devLoader)
        print('\nTest set: Loss: {:.4f}, Acc: {:.4f}, Prec: {:.4f}, Rec: {:.4f}\n'.format(
            test_loss, acc, prec, rec))
        testF.write('{},{},{},{},{}\n'.format(epoch, test_loss, acc, prec, rec))
        testF.flush()

def adjust_opt(optAlg, optimizer, epoch):
    if optAlg == 'sgd':
        if epoch < 150: lr = 1e-1
        elif epoch == 150: lr = 1e-2
        elif epoch == 225: lr = 1e-3
        else: return

        for param_group in optimizer.param_groups:
            param_group['lr'] = lr

def unfreeze_weights(pretrained, model, epoch):
    if (pretrained) and epoch > (100):
        for param in model.features.parameters():
            param.require_grad = True

if __name__ == '__main__':
    main()
