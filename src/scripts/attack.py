'''Test CIFAR10 robustness with PyTorch.'''
import torch
import torch.nn as nn
import torch.optim as optim
import torch.backends.cudnn as cudnn
import numpy as np
import json
import os
import argparse
import pickle
import logging
import sys
from torchsummary import summary
from cleverhans.utils import random_targets, to_categorical

sys.path.insert(0, ".")
sys.path.insert(0, "./adversarial_robustness_toolbox")

from src.datasets.train_val_test_data_loaders import get_test_loader, get_normalized_tensor
from src.datasets.utils import get_dataset_inds, get_mini_dataset_inds
from src.datasets.tta_utils import get_tta_transforms
from src.utils import boolean_string, set_logger, get_image_shape
from src.models.utils import get_strides, get_conv1_params, get_model
from src.models import MLP, ResnetMlpStudent
from src.attacks.tta_whitebox_pgd import TTAGrayboxPGD
from src.attacks.bpda import BPDA
from src.classifiers.pytorch_tta_classifier import PyTorchTTAClassifier
from src.classifiers.substitute_classifier import SubstituteClassifier
from src.classifiers.hybrid_classifier import HybridClassifier

from art.attacks.evasion import FastGradientMethod, ProjectedGradientDescent, DeepFool, SaliencyMapMethod, \
    CarliniL2Method, CarliniLInfMethod, ElasticNet, SquareAttack, BoundaryAttack

parser = argparse.ArgumentParser(description='Generating adversarial images using ART')
parser.add_argument('--checkpoint_dir', default='/tmp/adversarial_robustness/cifar10/resnet34/regular/resnet34_00', type=str, help='checkpoint dir')
parser.add_argument('--checkpoint_file', default='ckpt.pth', type=str, help='checkpoint path file name')
parser.add_argument('--attack', default='fgsm', type=str, help='attack: fgsm, jsma, cw, deepfool, pgd, square,'
                                                               'boundary, bpda, adaptive_pgd, adaptive_sqiare')
parser.add_argument('--targeted', default=True, type=boolean_string, help='use trageted attack')
parser.add_argument('--attack_dir', default='fgsm2', type=str, help='attack directory')
parser.add_argument('--batch_size', default=100, type=int, help='batch size')
parser.add_argument('--num_workers', default=20, type=int, help='Data loading threads')

# for FGSM/PGD/CW_Linf/adaptive_pgd/square/bpda:
parser.add_argument('--eps'     , default=0.031, type=float, help='maximum Linf deviation from original image')
parser.add_argument('--eps_step', default=0.007, type=float, help='step size of each adv iteration')

# for adaptive_pgd, bpda:
parser.add_argument('--max_iter', default=100, type=int, help='Number of TTAs to use in the PGD whitebox attack')
parser.add_argument('--tta_size', default=256, type=int, help='Number of TTAs to use in the PGD whitebox attack')

parser.add_argument('--mode', default='null', type=str, help='to bypass pycharm bug')
parser.add_argument('--port', default='null', type=str, help='to bypass pycharm bug')

args = parser.parse_args()

# for reproduce
# torch.manual_seed(9)
# random.seed(9)
# np.random.seed(9)
# rand_gen = np.random.RandomState(seed=12345)
if args.attack in ['deepfool', 'square']:
    assert not args.targeted

device = 'cuda' if torch.cuda.is_available() else 'cpu'
with open(os.path.join(args.checkpoint_dir, 'commandline_args.txt'), 'r') as f:
    train_args = json.load(f)

CHECKPOINT_PATH = os.path.join(args.checkpoint_dir, args.checkpoint_file)
ATTACK_DIR = os.path.join(args.checkpoint_dir, args.attack_dir)
os.makedirs(os.path.join(ATTACK_DIR, 'inds'), exist_ok=True)
batch_size = args.batch_size

log_file = os.path.join(ATTACK_DIR, 'log.log')
set_logger(log_file)
logger = logging.getLogger()

# Data
dataset = train_args['dataset']
logger.info('==> Preparing data..')
testloader = get_test_loader(
    dataset=dataset,
    batch_size=batch_size,
    num_workers=args.num_workers,
    pin_memory=device=='cuda'
)
img_shape = get_image_shape(dataset)
classes = testloader.dataset.classes
all_test_size  = len(testloader.dataset)

# Model
logger.info('==> Building model..')
conv1 = get_conv1_params(dataset)
strides = get_strides(dataset)
global_state = torch.load(CHECKPOINT_PATH, map_location=torch.device(device))
if 'best_net' in global_state:
    global_state = global_state['best_net']
net = get_model(train_args['net'])(num_classes=len(classes), activation=train_args['activation'], conv1=conv1, strides=strides)
net = net.to(device)
net.load_state_dict(global_state)
net.eval()
# summary(net, (img_shape[2], img_shape[0], img_shape[1]))
if device == 'cuda':
    # net = torch.nn.DataParallel(net)
    cudnn.benchmark = True

criterion = nn.CrossEntropyLoss()
optimizer = optim.SGD(
    net.parameters(),
    lr=train_args['lr'],
    momentum=train_args['mom'],
    weight_decay=0.0,  # train_args['wd'],
    nesterov=train_args['mom'] > 0)

X_test = get_normalized_tensor(testloader, img_shape, batch_size)
y_test = np.asarray(testloader.dataset.targets)

classifier = PyTorchTTAClassifier(model=net, clip_values=(0, 1), loss=criterion,
                                  optimizer=optimizer, input_shape=(img_shape[2], img_shape[0], img_shape[1]),
                                  nb_classes=len(classes), fields=['logits'])

y_test_logits = classifier.predict(X_test, batch_size=batch_size)
y_test_preds = y_test_logits.argmax(axis=1)
test_acc = np.sum(y_test_preds == y_test) / all_test_size
logger.info('Accuracy on benign test examples: {}%'.format(test_acc * 100))

def get_sub_model_classifier():
    # load new classifier with the two Resnet + mlp
    SUB_MODEL_PATH = os.path.join(args.checkpoint_dir, 'random_forest', 'sub_model', 'ckpt.pth')
    mlp_state = torch.load(SUB_MODEL_PATH, map_location=torch.device(device))
    mlp_state = mlp_state['best_net']

    mlp = MLP(len(classes))
    mlp.load_state_dict(mlp_state)
    sub_net = ResnetMlpStudent(net, mlp)
    sub_net.to(device)
    sub_net.eval()

    sub_classifier = SubstituteClassifier(model=sub_net, clip_values=(0, 1), loss=criterion,
                                          optimizer=optimizer, input_shape=(img_shape[2], img_shape[0], img_shape[1]),
                                          nb_classes=len(classes), fields=['logits'], tta_size=args.tta_size,
                                          tta_transforms=get_tta_transforms(dataset))
    return sub_classifier

def get_hybrid_classifier():
    rf_model_path = os.path.join(args.checkpoint_dir, 'random_forest', 'random_forest_classifier.pkl')
    with open(rf_model_path, "rb") as f:
        rf_model = pickle.load(f)
    rf_model.n_jobs = 1  # overwrite
    rf_model.verbose = 0
    tta_args = {'gaussian_std': 0.005, 'soft_transforms': False, 'clip_inputs': False, 'tta_size': 256, 'num_workers': args.num_workers}
    hybrid_classifier = HybridClassifier(
        dnn_model=net,
        rf_model=rf_model,
        dataset=dataset,
        tta_args=tta_args,
        input_shape=(img_shape[2], img_shape[0], img_shape[1]),
        nb_classes=len(classes),
        clip_values=(0, 1),
        fields=['logits'],
        tta_dir=None
    )
    return hybrid_classifier

# attack
# creating targeted labels
if args.targeted:
    tgt_file = os.path.join(ATTACK_DIR, 'y_test_adv.npy')
    if not os.path.isfile(tgt_file):
        y_test_targets = random_targets(y_test, len(classes))
        y_test_adv = y_test_targets.argmax(axis=1)
        np.save(os.path.join(ATTACK_DIR, 'y_test_adv.npy'), y_test_adv)
    else:
        y_test_adv = np.load(os.path.join(ATTACK_DIR, 'y_test_adv.npy'))
        y_test_targets = to_categorical(y_test_adv, nb_classes=len(classes))
else:
    y_test_adv = None
    y_test_targets = None

if args.attack == 'fgsm':
    attack = FastGradientMethod(
        estimator=classifier,
        norm=np.inf,
        eps=args.eps,
        eps_step=args.eps_step,
        targeted=args.targeted,
        num_random_init=0,
        batch_size=batch_size
    )
elif args.attack == 'pgd':
    attack = ProjectedGradientDescent(
        estimator=classifier,
        norm=np.inf,
        eps=args.eps,
        eps_step=args.eps_step,
        targeted=args.targeted,
        batch_size=batch_size
    )
elif args.attack == 'adaptive_pgd':
    attack = TTAGrayboxPGD(
        estimator=classifier,
        norm=np.inf,
        eps=args.eps,
        eps_step=args.eps_step,
        max_iter=args.max_iter,
        targeted=args.targeted,
        batch_size=batch_size,
        tta_transforms=get_tta_transforms(dataset),
        tta_size=args.tta_size
    )
elif args.attack == 'deepfool':
    attack = DeepFool(
        classifier=classifier,
        max_iter=50,
        epsilon=0.02,
        nb_grads=len(classes),
        batch_size=batch_size
    )
elif args.attack == 'jsma':
    attack = SaliencyMapMethod(
        classifier=classifier,
        theta=1.0,
        gamma=0.01,
        batch_size=batch_size
    )
elif args.attack == 'cw':
    attack = CarliniL2Method(
        classifier=classifier,
        confidence=0.8,
        targeted=args.targeted,
        initial_const=0.1,
        batch_size=batch_size
    )
elif args.attack == 'cw_Linf':
    attack = CarliniLInfMethod(
        classifier=classifier,
        confidence=0.8,
        targeted=args.targeted,
        batch_size=batch_size,
        eps=args.eps
    )
elif args.attack == 'square':
    attack = SquareAttack(
        estimator=classifier,
        norm=np.inf,
        eps=args.eps,
        batch_size=batch_size
    )
elif args.attack == 'boundary':
    attack = BoundaryAttack(
        estimator=classifier,
        batch_size=batch_size,
        targeted=args.targeted
    )
elif args.attack == 'bpda':
    sub_classifier = get_sub_model_classifier()
    attack = BPDA(
        estimator=sub_classifier,
        norm=np.inf,
        eps=args.eps,
        eps_step=args.eps_step,
        max_iter=args.max_iter,
        targeted=args.targeted,
        batch_size=batch_size,
        tta_transforms=get_tta_transforms(dataset)
    )
elif args.attack == 'adaptive_square':
    hybrid_classifier = get_hybrid_classifier()
    attack = SquareAttack(
        estimator=hybrid_classifier,
        norm=np.inf,
        eps=args.eps,
        batch_size=batch_size,
        max_iter=args.max_iter
    )
elif args.attack == 'adaptive_boundary':
    hybrid_classifier = get_hybrid_classifier()
    attack = BoundaryAttack(
        estimator=hybrid_classifier,
        batch_size=batch_size,
        targeted=args.targeted,
        max_iter=args.max_iter
    )
else:
    err_str = 'Attack {} is not supported'.format(args.attack)
    logger.error(err_str)
    raise AssertionError(err_str)

dump_args = args.__dict__.copy()
dump_args['attack_params'] = {}
for param in attack.attack_params:
    if param in attack.__dict__.keys():
        if isinstance(attack.__dict__[param], (float, bool, str, int)):
            dump_args['attack_params'][param] = attack.__dict__[param]
with open(os.path.join(ATTACK_DIR, 'attack_args.txt'), 'w') as f:
    json.dump(dump_args, f, indent=2)

# Amending inds to attack only subset for some attacks heavy attacks:
val_inds, test_inds = get_dataset_inds(dataset)
mini_val_inds, mini_test_inds = get_mini_dataset_inds(dataset)
X_adv_init = None

if args.attack == 'boundary':
    # we use few test inds because this attack is very time consuming
    assert args.targeted, 'This code supports only targeted boundary attack'

    init_inds_file = os.path.join(ATTACK_DIR, 'init_inds.npy')
    if not os.path.isfile(init_inds_file):
        # generate X_adv_init
        init_inds = []
        for i in range(X_test.shape[0]):
            permitted_inds = np.where(y_test == y_test_adv[i])[0]
            ind = np.random.choice(permitted_inds, 1)[0]
            init_inds.append(ind)
        init_inds = np.asarray(init_inds)
        np.save(os.path.join(ATTACK_DIR, 'init_inds.npy'), init_inds)
    else:
        init_inds = np.load(os.path.join(ATTACK_DIR, 'init_inds.npy'))

    X_adv_init = X_test[init_inds]

    # Boundary attack is expensive. Using only mini val/test samples
    mini_inds = np.concatenate((mini_val_inds, mini_test_inds))
    mini_inds.sort()

    X_test            = X_test[mini_inds]
    y_test            = y_test[mini_inds]
    y_test_preds      = y_test_preds[mini_inds]
    y_test_adv        = y_test_adv[mini_inds]
    y_test_targets    = y_test_targets[mini_inds]
    X_adv_init        = X_adv_init[mini_inds]
elif args.attack in ['bpda', 'adaptive_square']:
    # for adaptive attack (expensive) we cannot defend against, so it is sufficient to calculate just the test
    X_test            = X_test[mini_test_inds]
    y_test            = y_test[mini_test_inds]
    y_test_preds      = y_test_preds[mini_test_inds]
    if args.targeted:
        y_test_adv        = y_test_adv[mini_test_inds]
        y_test_targets    = y_test_targets[mini_test_inds]

if not os.path.exists(os.path.join(ATTACK_DIR, 'X_test_adv.npy')):
    X_test_adv = attack.generate(x=X_test, y=y_test_targets, x_adv_init=X_adv_init)
    np.save(os.path.join(ATTACK_DIR, 'X_test_adv.npy'), X_test_adv)

    test_adv_logits = classifier.predict(X_test_adv, batch_size=batch_size)
    y_test_adv_preds = np.argmax(test_adv_logits, axis=1)
    np.save(os.path.join(ATTACK_DIR, 'y_test_adv_preds.npy'), y_test_adv_preds)
else:
    X_test_adv       = np.load(os.path.join(ATTACK_DIR, 'X_test_adv.npy'))
    y_test_adv_preds = np.load(os.path.join(ATTACK_DIR, 'y_test_adv_preds.npy'))

test_adv_accuracy = np.mean(y_test_adv_preds == y_test)
logger.info('Accuracy on adversarial test examples: {}%'.format(test_adv_accuracy * 100))
logger.info('Done.')
logger.handlers[0].flush()
