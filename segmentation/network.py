from segmentation.dataset import dirs_to_pandaframe, load_image_map_from_file, MaskDataset, compose, post_transforms
from albumentations import (HorizontalFlip, ShiftScaleRotate, Normalize, Resize, Compose, GaussNoise)
import gc
from collections.abc import Iterable
import torch
import torch.nn as nn
from torch.utils import data
import logging
from segmentation.settings import TrainSettings, PredictorSettings
import segmentation_models_pytorch as sm
from segmentation.dataset import label_to_colors, XMLDataset
from typing import Union
import numpy as np
from pagexml_mask_converter.pagexml_to_mask import MaskGenerator, MaskSetting, BaseMaskGenerator, MaskType, PCGTSVersion

from matplotlib import pyplot as plt

logger = logging.getLogger(__name__)
logFormatter = logging.Formatter("%(asctime)s [%(threadName)-12.12s] [%(levelname)-5.5s]  %(message)s")
console_logger = logging.StreamHandler()
console_logger.setFormatter(logFormatter)
console_logger.terminator = ""
logger.setLevel(logging.DEBUG)
logger.addHandler(console_logger)


def pad(tensor, factor=32):
    shape = list(tensor.shape)[2:]
    h_dif = factor - (shape[0] % factor)
    x_dif = factor - (shape[1] % factor)
    x_dif = x_dif if factor != x_dif else 0
    h_dif = h_dif if factor != h_dif else 0
    augmented_image = tensor
    if h_dif != 0 or x_dif != 0:
        augmented_image = torch.nn.functional.pad(input=tensor, pad=[0, x_dif, 0, h_dif])
    return augmented_image


def unpad(tensor, o_shape):
    output = tensor[:, :, :o_shape[0], :o_shape[1]]
    return output


def test(model, device, test_loader, criterion):
    model.eval()
    test_loss = 0
    correct = 0
    total = 0
    with torch.no_grad():
        for idx, (data, target) in enumerate(test_loader):
            data, target = data.to(device), target.to(device, dtype=torch.int64)
            shape = list(data.shape)[2:]
            padded = pad(data, 32)

            input = padded.float()

            output = model(input)
            output = unpad(output, shape)
            test_loss += criterion(output, target)
            _, predicted = torch.max(output.data, 1)

            total += target.nelement()
            correct += predicted.eq(target.data).sum().item()
            logger.info('\r Image [{}/{}'.format(idx * len(data), len(test_loader.dataset)))

    test_loss /= len(test_loader.dataset)

    logger.info('\nTest set: Average loss: {:.4f}, Accuracy: {}/{} ({:.6f}%)\n'.format(
        test_loss, correct, len(test_loader.dataset),
        100. * correct / total))
    return 100. * correct / total


def train(model, device, train_loader, optimizer, epoch, criterion, accumulation_steps=8, color_map=None):
    def debug(mask, target, original, color_map):
        if color_map is not None:
            from matplotlib import pyplot as plt
            mean = [0.485, 0.456, 0.406]
            stds = [0.229, 0.224, 0.225]
            mask = torch.argmax(mask, dim=1)
            mask = torch.squeeze(mask)
            original = original.permute(0, 2, 3, 1)
            original = torch.squeeze(original).cpu().numpy()
            original = original * stds
            original = original + mean
            original = original * 255
            original = original.astype(int)
            f, ax = plt.subplots(1, 3, True, True)
            target = torch.squeeze(target)
            ax[0].imshow(label_to_colors(mask=target, colormap=color_map))
            ax[1].imshow(label_to_colors(mask=mask, colormap=color_map))
            ax[2].imshow(original)

            plt.show()

    model.train()
    total_train = 0
    correct_train = 0
    for batch_idx, (data, target) in enumerate(train_loader):

        data, target = data.to(device), target.to(device, dtype=torch.int64)

        shape = list(data.shape)[2:]
        padded = pad(data, 32)

        input = padded.float()

        output = model(input)
        output = unpad(output, shape)
        loss = criterion(output, target)
        loss = loss / accumulation_steps
        loss.backward()
        _, predicted = torch.max(output.data, 1)
        total_train += target.nelement()
        correct_train += predicted.eq(target.data).sum().item()
        train_accuracy = 100 * correct_train / total_train
        logger.info(
            '\r Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}\tAccuracy: {:.6f}'.format(epoch, batch_idx * len(data),
                                                                                          len(train_loader.dataset),
                                                                                          100. * batch_idx / len(
                                                                                              train_loader),
                                                                                          loss.item(),
                                                                                          train_accuracy)),
        if (batch_idx + 1) % accumulation_steps == 0:  # Wait for several backward steps
            debug(output, target, data, color_map)
            if isinstance(optimizer, Iterable):  # Now we can do an optimizer step
                for opt in optimizer:
                    opt.step()
            else:
                optimizer.step()
            model.zero_grad()  # Reset gradients tensors
        gc.collect()


def get_model(architecture, kwargs):
    architecture = architecture.get_architecture()(**kwargs)
    return architecture


class Network(object):

    def __init__(self, settings: Union[TrainSettings, PredictorSettings], color_map=None):
        self.settings = settings

        if isinstance(settings, PredictorSettings):
            self.settings.PREDICT_DATASET.preprocessing = sm.encoders.get_preprocessing_fn(self.settings.ENCODER)
        elif isinstance(settings, TrainSettings):
            self.settings.TRAIN_DATASET.preprocessing = sm.encoders.get_preprocessing_fn(self.settings.ENCODER)
            self.settings.VAL_DATASET.preprocessing = sm.encoders.get_preprocessing_fn(self.settings.ENCODER)
        device = "cuda:0" if torch.cuda.is_available() else "cpu"
        self.device = torch.device(device)
        self.model_params = self.settings.ARCHITECTURE.get_architecture_params()
        self.model_params['classes'] = self.settings.CLASSES
        self.model_params['decoder_use_batchnorm'] = False
        self.model_params['encoder_name'] = self.settings.ENCODER
        self.model = get_model(self.settings.ARCHITECTURE, self.model_params)
        if self.settings.MODEL_PATH:
            try:
                self.model.load_state_dict(torch.load(self.settings.MODEL_PATH, map_location=torch.device(device)))
            except Exception:
                logger.warning('Could not load model weights, ... Skipping\n')

        self.color_map = color_map  # Optional for visualisation of mask data
        self.model.to(self.device)

    def train(self):

        if not isinstance(self.settings, TrainSettings):
            logger.warning('Settings is of type: {}. Pass settings to network object of type Train to train'.format(
                str(type(self.settings))))
            return

        criterion = nn.CrossEntropyLoss()
        self.model.float()
        opt = self.settings.OPTIMIZER.getOptimizer()
        try:
            optimizer1 = opt(self.model.encoder.parameters(), lr=self.settings.LEARNINGRATE_ENCODER)
            optimizer2 = opt(self.model.decoder.parameters(), lr=self.settings.LEARNINGRATE_DECODER)
            optimizer3 = opt(self.model.segmentation_head.parameters(), lr=self.settings.LEARNINGRATE_SEGHEAD)
            optimizer = [optimizer1, optimizer2, optimizer3]
        except:
            optimizer = opt(self.model.parameters(), lr=self.settings.LEARNINGRATE_SEGHEAD)

        train_loader = data.DataLoader(dataset=self.settings.TRAIN_DATASET, batch_size=self.settings.TRAIN_BATCH_SIZE,
                                       shuffle=True, num_workers=self.settings.PROCESSES)
        val_loader = data.DataLoader(dataset=self.settings.VAL_DATASET, batch_size=self.settings.VAL_BATCH_SIZE,
                                     shuffle=False)
        highest_accuracy = 0
        logger.info(str(self.model) + "\n")
        logger.info(str(self.model_params) + "\n")
        logger.info('Training started ...\n"')
        for epoch in range(1, self.settings.EPOCHS):
            train(self.model, self.device, train_loader, optimizer, epoch, criterion,
                  accumulation_steps=self.settings.BATCH_ACCUMULATION,
                  color_map=self.color_map)
            accuracy = test(self.model, self.device, val_loader, criterion=criterion)
            if self.settings.OUTPUT_PATH is not None:
                if accuracy > highest_accuracy:
                    logger.info('Saving model to {}\n'.format(self.settings.OUTPUT_PATH))
                    torch.save(self.model.state_dict(), self.settings.OUTPUT_PATH)
                    highest_accuracy = accuracy

    def predict(self):

        from torch.utils import data

        self.model.eval()

        if not isinstance(self.settings, PredictorSettings):
            logger.warning('Settings is of type: {}. Pass settings to network object of type Train to train'.format(
                str(type(self.settings))))
            return
        predict_loader = data.DataLoader(dataset=self.settings.PREDICT_DATASET,
                                         batch_size=1,
                                         shuffle=False, num_workers=self.settings.PROCESSES)
        import ttach as tta
        transforms = tta.Compose(
            [
                tta.HorizontalFlip(),
                tta.Scale(scales=[1]),
            ]
        )
        with torch.no_grad():
            for idx, (data, target) in enumerate(predict_loader):
                data, target = data.to(self.device), target.to(self.device, dtype=torch.int64)
                outputs = []
                o_shape = data.shape
                for transformer in transforms:
                    augmented_image = transformer.augment_image(data)
                    shape = list(augmented_image.shape)[2:]
                    padded = pad(augmented_image, 32)  ## 2**5

                    input = padded.float()
                    output = self.model(input)
                    output = unpad(output, shape)
                    reversed = transformer.deaugment_mask(output)
                    reversed = torch.nn.functional.interpolate(reversed, size=list(o_shape)[2:], mode="nearest")
                    print("original: {} input: {}, padded: {} unpadded {} output {}".format(str(o_shape),
                                                                                            str(shape), str(
                            list(augmented_image.shape)), str(list(output.shape)), str(list(reversed.shape))))
                    outputs.append(reversed)
                stacked = torch.stack(outputs)
                output = torch.mean(stacked, dim=0)
                outputs.append(output)

                def debug(mask, target, original, color_map):
                    if color_map is not None:
                        mean = [0.485, 0.456, 0.406]
                        stds = [0.229, 0.224, 0.225]
                        mask = torch.argmax(mask, dim=1)
                        mask = torch.squeeze(mask)
                        original = original.permute(0, 2, 3, 1)
                        original = torch.squeeze(original).cpu().numpy()
                        original = original * stds
                        original = original + mean
                        original = original * 255
                        original = original.astype(int)
                        extract_baselines(mask, original=original)

                        f, ax = plt.subplots(1, 3, True, True)
                        target = torch.squeeze(target)
                        ax[0].imshow(label_to_colors(mask=target, colormap=color_map))
                        ax[1].imshow(label_to_colors(mask=mask, colormap=color_map))
                        ax[2].imshow(original)
                        ax[2].imshow(label_to_colors(mask=mask, colormap=color_map), cmap='jet', alpha=0.5)

                        plt.show()

                debug(output, target, data, self.color_map)

                out = output.data.cpu().numpy()
                out = np.transpose(out, (0, 2, 3, 1))
                out = np.squeeze(out)

                def plot(outputs):
                    list_out = []
                    for ind, x in enumerate(outputs):
                        mask = torch.argmax(x, dim=1)
                        mask = torch.squeeze(mask)
                        list_out.append(label_to_colors(mask=mask, colormap=self.color_map))
                    list_out.append(label_to_colors(mask=torch.squeeze(target), colormap=self.color_map))
                    plot_list(list_out)

                plot(outputs)
                yield out

    def predict_single_image(self):
        pass


def extract_baselines(image_map: np.array, base_line_index=1, base_line_border_index=2, original=None):
    # from skimage import measure
    from scipy.ndimage.measurements import label

    base_ind = np.where(image_map == base_line_index)
    base_border_ind = np.where(image_map == base_line_border_index)

    baseline = np.zeros(image_map.shape)
    baseline_border = np.zeros(image_map.shape)
    baseline[base_ind] = 1
    baseline_border[base_border_ind] = 1
    baseline_ccs, n_baseline_ccs = label(baseline)
    t = list(range(n_baseline_ccs))

    baseline_ccs = [np.where(baseline_ccs == x) for x in range(1, n_baseline_ccs + 1)]
    baseline_border_ccs, n_baseline_border_ccs = label(baseline_border)
    baseline_border_ccs = [np.where(baseline_border_ccs == x) for x in range(1, n_baseline_border_ccs + 1)]
    print(n_baseline_ccs)
    print(n_baseline_border_ccs)

    class Cc_with_type(object):
        def __init__(self, cc, type):
            self.cc = cc
            index_min = np.where(cc[1] == min(cc[1]))[0]

            self.cc_left = (cc[0][index_min][0], cc[1][index_min][0])
            index_max = np.where(cc[1] == max(cc[1]))[0]

            self.cc_right = (cc[0][index_max][0], cc[1][index_max][0])
            self.type = type

        def __lt__(self, other):
            return self.cc < other

    baseline_ccs = [Cc_with_type(x, 'baseline') for x in baseline_ccs if len(x[0]) > 10]

    baseline_border_ccs = [Cc_with_type(x, 'baseline_border') for x in baseline_border_ccs if len(x[0]) > 10]

    all_ccs = baseline_ccs + baseline_border_ccs

    def calculate_distance_matrix(ccs, length=50):
        distance_matrix = np.zeros((len(ccs), len(ccs)))
        vertical_distance = 10
        for ind1, x in enumerate(ccs):
            for ind2, y in enumerate(ccs):
                if x is y:
                    distance_matrix[ind1, ind2] = 0
                else:
                    distance = 0
                    same_type = 1 if x.type == y.type else 100

                    def left(x, y):
                        return x.cc_left[1] > y.cc_right[1]

                    def right(x, y):
                        return x.cc_right[1] < y.cc_left[1]

                    if left(x, y):
                        distance = x.cc_left[1] - y.cc_right[1]
                        v_distance = vertical_distance
                        if distance < 1 / 5 * length:
                            v_distance = vertical_distance / 2
                        elif distance < length:
                            v_distance = vertical_distance * 2 / 3
                        if not abs(x.cc_left[0] - y.cc_right[0]) < v_distance:
                            distance = distance + abs(x.cc_left[0] - y.cc_right[0]) * 5
                    elif right(x, y):
                        distance = y.cc_left[1] - x.cc_right[1]

                        v_distance = vertical_distance
                        if distance < 1 / 5 * length:
                            v_distance = vertical_distance / 2
                        elif distance < length:
                            v_distance = vertical_distance * 2 / 3
                        if not abs(x.cc_left[0] - y.cc_right[0]) < v_distance:
                            if not abs(x.cc_right[0] - y.cc_left[0]) < v_distance:
                                distance = distance + abs(y.cc_left[0] - x.cc_right[0]) * 5
                    else:
                        print('same object?')
                        distance = 99999

                    distance_matrix[ind1, ind2] = distance * same_type
        return distance_matrix

    matrix = calculate_distance_matrix(all_ccs)

    import sys
    import numpy
    print(matrix)
    from sklearn.cluster import DBSCAN
    t = DBSCAN(eps=50, min_samples=1, metric="precomputed").fit(matrix)
    debug_image = np.zeros(image_map.shape)
    for ind, x in enumerate(all_ccs):
        debug_image[x.cc] = t.labels_[ind]

    ccs= []
    for x in np.unique(t.labels_):
        ind = np.where(t.labels_ == x)
        line = []
        for d in ind[0]:
            line.append(all_ccs[d].cc)
        ccs.append((np.concatenate([x[0] for x in line]), np.concatenate([x[1] for x in line])))
    print(ccs)
    ccs = [list(zip(x[1][0], x[1][1])) for x in ccs]
    from itertools import chain
    #ccs = [list(zip(z.cc[0], z.cc[1])) for x in ccs for z in x]

   # print(ccs)
    from typing import List
    from collections import defaultdict
    def normalize_connected_components(cc_list: List[List[int]]):
        # Normalize the CCs (line segments), so that the height of each cc is normalized to one pixel
        def normalize(point_list):
            normalized_cc_list = []
            for cc in point_list:
                cc_dict = defaultdict(list)
                for y, x in cc:
                    cc_dict[x].append(y)
                normalized_cc = []
                #for key, value in cc_dict.items():
                for key in sorted(cc_dict.keys()):
                    value = cc_dict[key]
                    normalized_cc.append([int(np.floor(np.mean(value) + 0.5)), key])
                normalized_cc_list.append(normalized_cc)
            return normalized_cc_list

        return normalize(cc_list)
    ccs = normalize_connected_components(ccs)


    plt.imshow(original)
    from PIL import Image, ImageDraw

    im = Image.fromarray(np.uint8(original))
    draw = ImageDraw.Draw(im)

    for x in ccs:
        t = list(chain.from_iterable(x))
        print(t)
        a = t[::-1]

        #t = list(zip(*x))
        #print(t)
        draw.line(a, fill=(255,0,0))
        pass
    im.show()
    from matplotlib import pyplot
    import cycler
    n = 15
    color = pyplot.cm.rainbow(np.linspace(0, 1, n))
    debug_image[debug_image == 0] = 255
    plt.imshow(debug_image, cmap="gist_ncar")
    plt.show()




    #print(t.labels_)
    #print("1")
    '''
    from typing import List
    import math
    def extract_baselines(all_ccs):

        class Polyline(object):
            def __init__(self, list_ccs):
                self.polyline: List[np.array] = list_ccs

            def __iter__(self):
                for x in self.polyline:
                    yield x

        def left(x, y):
            return x.cc_left[1] < y.cc_right[1]

        def right(x, y):
            return x.cc_right[1] > y.cc_left[1]

        def get_nearest_cc(cc: Cc_with_type, ccs, comperator):
            gl_distance = math.inf
            vertical_distance = 10
            nearest = None
            for x in ccs:
                if x is cc:
                    continue
                if comperator(cc, x):
                    if abs(cc.cc_left[0] - cc.cc_right[0]) < vertical_distance:
                      distance = abs(cc.cc_left[1] - x.cc_right[1])
                      if distance < gl_distance:
                        gl_distance = distance
                        nearest = x
            return nearest, gl_distance

        while True:
            for ind, x in enumerate(all_ccs):
                left_cc = get_nearest_cc(x, all_ccs, left)
                right_cc = get_nearest_cc(x, all_ccs, right)
    '''


def plot_list(lsit):
    import matplotlib.pyplot as plt
    import numpy as np
    print(len(lsit))
    images_per_row = 4
    rows = int(np.ceil(len(lsit) / images_per_row))
    f, ax = plt.subplots(rows, images_per_row, True, True)
    ind = 0
    row = 0
    for x in lsit:
        if rows > 1:
            ax[row, ind].imshow(x)
        else:
            ax[ind].imshow(x)
        ind += 1
        if ind == images_per_row:
            row += 1
            ind = 0

    plt.show()

    def show_images(images, cols=1, titles=None):
        """Display a list of images in a single figure with matplotlib.

        Parameters
        ---------
        images: List of np.arrays compatible with plt.imshow.

        cols (Default = 1): Number of columns in figure (number of rows is
                            set to np.ceil(n_images/float(cols))).

        titles: List of titles corresponding to each image. Must have
                the same length as titles.
        """
        assert ((titles is None) or (len(images) == len(titles)))
        n_images = len(images)
        if titles is None: titles = ['Image (%d)' % i for i in range(1, n_images + 1)]
        fig = plt.figure()
        for n, (image, title) in enumerate(zip(images, titles)):
            a = fig.add_subplot(cols, np.ceil(n_images / float(cols)), n + 1)
            if image.ndim == 2:
                plt.gray()
            plt.imshow(image)
            a.set_title(title)
        fig.set_size_inches(np.array(fig.get_size_inches()) * n_images)
        plt.show()

    # show_images(images=lsit, cols=1)


if __name__ == '__main__':
    '''
    'https://github.com/catalyst-team/catalyst/blob/master/examples/notebooks/segmentation-tutorial.ipynb'
    a = dirs_to_pandaframe(
        ['/home/alexander/Dokumente/dataset/READ-ICDAR2019-cBAD-dataset/dataset-test/train/images/'],
        ['/home/alexander/Dokumente/dataset/READ-ICDAR2019-cBAD-dataset/dataset-test/train/masks/'])

    b = dirs_to_pandaframe(
        ['/home/alexander/Dokumente/dataset/READ-ICDAR2019-cBAD-dataset/dataset-test/test/images/'],
        ['/home/alexander/Dokumente/dataset/READ-ICDAR2019-cBAD-dataset/dataset-test/test/masks/']
    )
    b = b[:20]
    map = load_image_map_from_file(
        '/home/alexander/Dokumente/dataset/READ-ICDAR2019-cBAD-dataset/dataset-test/image_map.json')
    dt = MaskDataset(a, map, preprocessing=None, transform=compose([post_transforms()]))
    d_test = MaskDataset(b, map, preprocessing=None, transform=compose([post_transforms()]))
    '''
    a = dirs_to_pandaframe(
        ['/home/alexander/Dokumente/dataset/READ-ICDAR2019-cBAD-dataset/train/image/'],
        ['/home/alexander/Dokumente/dataset/READ-ICDAR2019-cBAD-dataset/train/page/'])

    b = dirs_to_pandaframe(
        ['/home/alexander/Dokumente/dataset/READ-ICDAR2019-cBAD-dataset/test/image/'],
        ['/home/alexander/Dokumente/dataset/READ-ICDAR2019-cBAD-dataset/test/page/'])

    c = dirs_to_pandaframe(
        ['/home/alexander/Dokumente/HBR2013/images/'],
        ['/home/alexander/Dokumente/HBR2013/masks/']
    )
    map = load_image_map_from_file(
        '/home/alexander/Dokumente/dataset/READ-ICDAR2019-cBAD-dataset/dataset-test/image_map.json')
    from segmentation.dataset import base_line_transform

    settings = MaskSetting(MASK_TYPE=MaskType.BASE_LINE, PCGTS_VERSION=PCGTSVersion.PCGTS2013, LINEWIDTH=5,
                           BASELINELENGTH=10)
    dt = XMLDataset(a, map, transform=compose([base_line_transform()]), mask_generator=MaskGenerator(settings=settings))
    d_test = XMLDataset(b, map, transform=compose([base_line_transform()]),
                        mask_generator=MaskGenerator(settings=settings))
    d_predict = MaskDataset(c, map, )  # transform=compose([base_line_transform()]))
    from segmentation.settings import TrainSettings

    setting = TrainSettings(CLASSES=len(map), TRAIN_DATASET=dt, VAL_DATASET=d_test, OUTPUT_PATH="model.torch3",
                            MODEL_PATH='/home/alexander/Dokumente/dataset/READ-ICDAR2019-cBAD-dataset/model_multi.torch')
    p_setting = PredictorSettings(CLASSES=len(map), PREDICT_DATASET=d_predict,
                                  MODEL_PATH='/home/alexander/Dokumente/dataset/READ-ICDAR2019-cBAD-dataset/model.torch')
    trainer = Network(p_setting, color_map=map)
    for x in trainer.predict():
        print(x.shape)
