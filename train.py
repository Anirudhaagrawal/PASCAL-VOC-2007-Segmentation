import gc
import time
import argparse

import numpy as np
import torchvision.transforms as standard_transforms
import yaml
from torch.optim.lr_scheduler import CosineAnnealingWarmRestarts
from torch.utils.data import DataLoader
from tqdm import tqdm

import util
import voc
from basic_fcn import *
from new_arch import *
from resnet import *
from resnet50 import *
from resnet_2model_skip_res_cat import *
from unet import *
from resnet_leaky_relu import *
from resnet_skip_residual import *


class MaskToTensor(object):
    def __call__(self, img):
        return torch.from_numpy(np.array(img, dtype=np.int32)).long()


def init_weights(m):
    if isinstance(m, nn.Conv2d) or isinstance(m, nn.ConvTranspose2d):
        torch.nn.init.xavier_uniform_(m.weight.data)
        torch.nn.init.normal_(m.bias.data)  # xavier not applicable for biases


def init_weights_transfer_learning(m):
    if isinstance(m, nn.ConvTranspose2d):
        torch.nn.init.xavier_uniform_(m.weight.data)
        torch.nn.init.normal_(m.bias.data)  # xavier not applicable for biases


def load_constants(config_name):
    config = yaml.load(open('configs/' + config_name, 'r'), Loader=yaml.SafeLoader)
    cosine_annealing = config['cosine_annealing']
    random_transforms = config['random_transforms']
    use_class_weights = config['class_imbalance_fix']
    epochs = config['epochs']
    batch_size = config['batch_size']
    model_type = config['model_type']
    model_identifier = config['model_identifier']
    freeze_encoder = config['freeze_encoder']
    print(f'cosine annealing:\t{cosine_annealing}')
    print(f'random transforms:\t{random_transforms}')
    print(f'use class weights:\t{use_class_weights}')
    print(f'model type:\t\t\t{model_type}')
    print(f'epochs:\t\t\t\t{epochs}')
    print(f'batch size:\t\t\t{batch_size}')
    print(f'freeze_encoder:\t\t\t{freeze_encoder}')
    return cosine_annealing, random_transforms, use_class_weights, epochs, batch_size, model_type, model_identifier, freeze_encoder


def get_model_optimizer_scheduler(cosine_annealing, random_transforms, use_class_weights, epochs, batch_size,
                                  model_type, model_identifier, freeze_encoder):
    n_class = 21

    mean_std = ([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
    input_transform = standard_transforms.Compose([
        standard_transforms.ToTensor(),
        standard_transforms.Normalize(*mean_std),

    ])
    target_transform = MaskToTensor()

    train_dataset = voc.VOC('train', random_transforms, transform=input_transform, target_transform=target_transform)
    val_dataset = voc.VOC('val', False, transform=input_transform, target_transform=target_transform)
    test_dataset = voc.VOC('test', False, transform=input_transform, target_transform=target_transform)

    train_loader = DataLoader(dataset=train_dataset, batch_size=batch_size, shuffle=True)
    val_loader = DataLoader(dataset=val_dataset, batch_size=batch_size, shuffle=False)
    test_loader = DataLoader(dataset=test_dataset, batch_size=batch_size, shuffle=False)
    if model_type.lower() == "unet":
        fcn_model = UNet(n_class=n_class)
        fcn_model.apply(init_weights)
    elif model_type.lower() == "resnet":
        fcn_model = Resnet(n_class=n_class, freeze_encoder=freeze_encoder)
        fcn_model.apply(init_weights_transfer_learning)
    elif model_type.lower() == "new_arch":
        fcn_model = New_Arch(n_class=n_class)
        fcn_model.apply(init_weights)
    elif model_type.lower() == "resnet50":
        fcn_model = Resnet50(n_class=n_class, freeze_encoder=freeze_encoder)
        fcn_model.apply(init_weights_transfer_learning)
    elif model_type.lower() == "resnet_leaky":
        fcn_model = ResnetLeaky(n_class=n_class, freeze_encoder=freeze_encoder)
        fcn_model.apply(init_weights_transfer_learning)
    elif model_type.lower() == "resnet_2model_skip_res_cat":
        fcn_model = Resnet2ModelSkipResCat(n_class=n_class, freeze_encoder=freeze_encoder)
        fcn_model.apply(init_weights_transfer_learning)
    elif model_type.lower() == "resnet_skip_residual":
        fcn_model = Resnet_Skip_Residual(n_class=n_class, freeze_encoder=freeze_encoder)
        fcn_model.apply(init_weights_transfer_learning)
    else:
        fcn_model = FCN(n_class=n_class)
        fcn_model.apply(init_weights)

    device = 'cuda' if torch.cuda.is_available() else 'cpu'  # TODO determine which device to use (cuda or cpu)

    device = torch.device(device)

    optimizer = torch.optim.Adam(fcn_model.parameters(), lr=0.001)
    if cosine_annealing:
        scheduler = CosineAnnealingWarmRestarts(optimizer, T_0=20, T_mult=1)
    weights = train_dataset.get_class_weights()
    if use_class_weights:
        class_weights = torch.FloatTensor(weights)
        criterion = nn.CrossEntropyLoss(
            weight=class_weights)
        class_weights = class_weights.to(device)
    else:
        criterion = nn.CrossEntropyLoss()

    fcn_model = fcn_model.to(device=device)  # TODO transfer the model to the device
    return fcn_model, optimizer, scheduler, criterion, train_loader, val_loader, test_loader, device, epochs, model_identifier, class_weights


def train(save_location, fcn_model, optimizer, scheduler, criterion, train_loader, val_loader, test_loader, device, epochs, model_identifier, class_weights):
    # ---------------------------------
    # Initialize network, progress bar,
    # arrays to record train/val accuracy
    # and loss, counter of bad epochs
    # ---------------------------------
    best_iou_score = 0.0
    val_loss = np.zeros(epochs)
    train_loss = np.zeros(epochs)
    mean_iou_scores = np.zeros(epochs)
    val_accuracy = np.zeros(epochs)
    early_stop_patience = 5
    early_stop = True  # flag to identify if early stopping is desired

    # number of consecutive epochs where
    # model performs worse
    bad_epochs = 0
    earlyStop = -1

    # loading bar
    training_pbar = tqdm(total=epochs, desc=f'Training Procedure', position=0)
    train_size = len(train_loader.dataset)

    # ------------------
    # Training Procedure
    # ------------------
    for epoch in range(epochs):
        inner_pbar = tqdm(total=train_size, desc=f'Training Epoch {epoch + 1}', position=0, leave=True)
        iters = len(train_loader)
        train_losses = []
        for iter, (inputs, labels) in enumerate(train_loader):
            optimizer.zero_grad()
            if use_class_weights:
                criterion = nn.CrossEntropyLoss(weight=class_weights)
            else:
                criterion = nn.CrossEntropyLoss()

            inputs = inputs.to(device)
            labels = labels.to(device)

            outputs = fcn_model.forward(inputs)

            loss = criterion(outputs, labels)
            train_losses.append(loss.item())

            loss.backward()
            optimizer.step()
            if cosine_annealing:
                scheduler.step(epoch + iter / iters)

            inner_pbar.update(train_loader.batch_size)
        train_loss[epoch] = np.mean(train_losses)
        inner_pbar.close()

        current_miou_score, current_accuracy, current_val_loss = val(epoch, fcn_model, criterion, val_loader, device, class_weights)
        val_loss[epoch] = current_val_loss
        mean_iou_scores[epoch] = current_miou_score
        val_accuracy[epoch] = current_accuracy

        if current_miou_score > best_iou_score:
            best_iou_score = current_miou_score
            path = save_location + 'model.pt'
            torch.save(fcn_model, path)
            # save the best model

        if epoch > 0 and early_stop and current_val_loss > val_loss[epoch - 1]:
            bad_epochs += 1
        else:
            bad_epochs = 0
        if bad_epochs > early_stop_patience:
            earlyStop = epoch
            print(f'Patience threshold reached ({early_stop_patience} epochs).')
            print(f'Early stopping after completing epoch {epoch + 1}.')
            break

        training_pbar.update(1)
    training_pbar.close()
    util.plots(train_loss, val_loss, val_accuracy, mean_iou_scores, earlyStop, saveLocation=save_location)


def val(epoch, fcn_model, criterion, val_loader, device, class_weights):
    fcn_model.eval()  # Put in eval mode (disables batchnorm/dropout) !

    losses = []
    mean_iou_scores = []
    accuracy = []
    with torch.no_grad():  # we don't need to calculate the gradient in the validation/testing
        val_size = len(val_loader.dataset)
        val_pbar = tqdm(total=val_size, desc=f'Validation Epoch {epoch + 1}', position=0, leave=True)
        for iter, (input, label) in enumerate(val_loader):
            input = input.to(device)
            output = fcn_model.forward(input)
            label = label.to('cpu')
            output = output.to('cpu')

            if use_class_weights:
                criterion = nn.CrossEntropyLoss(weight=class_weights.to('cpu'))
            else:
                criterion = nn.CrossEntropyLoss()
            loss = criterion(output, label)
            losses.append(loss.item())

            pred = output.argmax(dim=1)
            mean_iou_scores.append(util.iou(pred, label))
            accuracy.append(util.pixel_acc(pred, label))
            val_pbar.update(val_loader.batch_size)
        val_pbar.close()
    tqdm.write(f'Epoch\t{epoch + 1}')
    tqdm.write(f"loss\t{np.mean(losses)}")
    tqdm.write(f"IoU\t{np.mean(mean_iou_scores)}")
    tqdm.write(f"PA\t{np.mean(accuracy)}")

    fcn_model.train()  # TURNING THE TRAIN MODE BACK ON TO ENABLE BATCHNORM/DROPOUT!!

    return np.mean(mean_iou_scores), np.mean(accuracy), np.mean(losses)


def modelTest(save_location, test_loader, device, criterion, class_weights):
    path = save_location + 'model.pt'
    model = torch.load(path)
    model.eval()
    losses = []
    mean_iou_scores = []
    accuracy = []
    # fcn_model.eval()  # Put in eval mode (disables batchnorm/dropout) !
    i = 0
    with torch.no_grad():  # we don't need to calculate the gradient in the validation/testing
        test_size = len(test_loader.dataset)
        val_pbar = tqdm(total=test_size, desc=f'Testing', position=0, leave=True)
        for iter, (input, label) in enumerate(test_loader):
            input = input.to(device)
            output = model.forward(input)

            output = output.to('cpu')
            if use_class_weights:
                criterion = nn.CrossEntropyLoss(weight=class_weights.to('cpu'))
            else:
                criterion = nn.CrossEntropyLoss()
            loss = criterion(output, label)
            losses.append(loss.item())
            pred = output.argmax(dim=1)
            mean_iou_scores.append(util.iou(pred, label))
            accuracy.append(util.pixel_acc(pred, label))
            val_pbar.update(test_loader.batch_size)
            input = input.to('cpu')
            util.plot_predictions(input[0], label[0], pred[0], save_location, i)
            i = i + 1
        val_pbar.close()
    tqdm.write(f"loss\t{np.mean(losses)}")
    tqdm.write(f"IoU\t{np.mean(mean_iou_scores)}")
    tqdm.write(f"PA\t{np.mean(accuracy)}")


# TURNING THE TRAIN MODE BACK ON TO ENABLE BATCHNORM/DROPOUT!!


if __name__ == "__main__":
    import os

    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=str, default='config5a-3.yml',
                        help='Specify the config that you want to run')
    args = parser.parse_args()
    cosine_annealing, random_transforms, use_class_weights, epochs, batch_size, model_type, model_identifier, freeze_encoder = load_constants(
        args.config)
    fcn_model, optimizer, scheduler, criterion, train_loader, val_loader, test_loader, device, epochs, model_identifier, class_weights=get_model_optimizer_scheduler(cosine_annealing, random_transforms, use_class_weights, epochs, batch_size, model_type, model_identifier, freeze_encoder)
    path = 'Results'
    if not os.path.exists(path):
        os.mkdir(path)
    save_location = path + '/' + model_identifier
    val(0, fcn_model, criterion, val_loader, device, class_weights)  # show the accuracy before training
    # timekeeping
    start = time.time()

    train(save_location, fcn_model, optimizer, scheduler, criterion, train_loader, val_loader, test_loader, device, epochs, model_identifier, class_weights)
    modelTest(save_location, test_loader, device, criterion, class_weights)

    end = time.time()

    time_elapsed_ms = end - start
    print(f'Time elapsed:\t{time_elapsed_ms} seconds')

    # housekeeping
    gc.collect()
    torch.cuda.empty_cache()
