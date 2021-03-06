import itertools
import os
import string
import argparse

import pandas as pd

import torch
import torch.backends.cudnn as cudnn
import torch.utils.data
import torch.nn.functional as F
import numpy as np
from PIL import Image, ImageDraw

from utils import CTCLabelConverter, AttnLabelConverter
from dataset import RawDataset, AlignCollate
from model import Model

device = None


def demo(opt):
    """ model configuration """
    if 'CTC' in opt.Prediction:
        converter = CTCLabelConverter(opt.character)
    else:
        converter = AttnLabelConverter(opt.character)
    opt.num_class = len(converter.character)

    if opt.rgb:
        opt.input_channel = 3
    model = Model(opt)
    print('model input parameters', opt.imgH, opt.imgW, opt.num_fiducial, opt.input_channel, opt.output_channel,
          opt.hidden_size, opt.num_class, opt.batch_max_length, opt.Transformation, opt.FeatureExtraction,
          opt.SequenceModeling, opt.Prediction)
    try:
        model = torch.nn.DataParallel(model).to(device)
    except RuntimeError:
        raise RuntimeError(device)

    # load model
    print('loading pretrained model from %s' % opt.saved_model)
    model.load_state_dict(torch.load(opt.saved_model, map_location=device))

    # prepare data. two demo images from https://github.com/bgshih/crnn#run-demo
    AlignCollate_demo = AlignCollate(imgH=opt.imgH, imgW=opt.imgW, keep_ratio_with_pad=opt.PAD)
    demo_data = RawDataset(root=opt.image_folder, opt=opt)  # use RawDataset
    demo_loader = torch.utils.data.DataLoader(
        demo_data, batch_size=opt.batch_size,
        shuffle=False,
        num_workers=int(opt.workers),
        collate_fn=AlignCollate_demo, pin_memory=True)

    # predict
    model.eval()
    with torch.no_grad():
        for image_tensors, image_path_list in demo_loader:
            batch_size = image_tensors.size(0)
            image = image_tensors.to(device)
            # For max length prediction
            length_for_pred = torch.IntTensor([opt.batch_max_length] * batch_size).to(device)
            text_for_pred = torch.LongTensor(batch_size, opt.batch_max_length + 1).fill_(0).to(device)

            if 'CTC' in opt.Prediction:
                preds = model(image, text_for_pred)

                # Select max probabilty (greedy decoding) then decode index to character
                preds_size = torch.IntTensor([preds.size(1)] * batch_size)
                _, preds_index = preds.max(2)
                # preds_index = preds_index.view(-1)
                preds_str = converter.decode(preds_index, preds_size)
            else:
                preds, alphas = model(image, text_for_pred, is_train=False)
                alphas = alphas.detach().cpu().numpy()
                if opt.batch_max_length == 1:
                    # select top_k probabilty (greedy decoding) then decode index to character
                    k = opt.topk
                    preds = F.softmax(preds, dim=2)
                    topk_prob, topk_id = preds.topk(k)
                    topk_id = topk_id.detach().cpu()[:, 0, :].unsqueeze(dim=1).numpy()  # (batch_size, topk)
                    # concat 3(['s']) to the end of ids
                    topk_s = np.ones_like(topk_id) * 3
                    topk_id = np.concatenate((topk_id, topk_s), axis=1)
                    topk_chars = converter.decode(topk_id, length_for_pred)
                    topk_probs = topk_prob.detach().cpu()[:, 0, :]  # (batch_size, topk)
                else:
                    # select max probabilty (greedy decoding) then decode index to character
                    k = opt.topk
                    # _, preds_index = preds.max(dim=2)
                    # preds_str = converter.decode(preds_index, length_for_pred)
                    preds = F.softmax(preds, dim=2)
                    topk_prob, topk_id = preds.topk(k, dim=2)
                    topk_id = topk_id.detach().cpu().numpy()  # (batch_size, topk)
                    topk_probs = topk_prob.detach().cpu()
                    topk_strs = converter.decode(topk_id, length_for_pred)

            if opt.batch_max_length == 1:
                log = open(f'./log_demo_result.csv', 'a', encoding='utf-8')
                # topk_probs = F.softmax(topk_probs, dim=-1)
                for img_name, pred, pred_max_prob in zip(image_path_list, topk_chars, topk_probs):
                    if 'Attn' in opt.Prediction:
                        pred = [p[:p.find('[s]')] for p in pred]  # prune after "end of sentence" token ([s])
                    print(img_name, end='')
                    log.write(img_name)
                    for pred_char, pred_prob in zip(pred, pred_max_prob):
                        print(',' + pred_char, end='')
                        print(',%.4f' % pred_prob, end='')
                        log.write(',' + pred_char)
                        log.write(',%.4f' % pred_prob)
                    print()
                    log.write('\n')
                log.close()
            else:
                log = open(f'./log_demo_result.txt', 'a', encoding='utf-8')
                dashed_line = '-' * 80
                head = f'{"image_path":25s}\t{"predicted_labels":25s}\tconfidence score'

                print(f'{dashed_line}\n{head}\n{dashed_line}')
                log.write(f'{dashed_line}\n{head}\n{dashed_line}\n')

                # preds_prob = F.softmax(preds, dim=2)
                # preds_max_prob, _ = preds_prob.max(dim=2)
                if 'Attn' in opt.Prediction:
                    for idx, (img_name, pred, pred_max_prob) in enumerate(zip(image_path_list, topk_strs, topk_probs)):
                        pred_EOS = pred[0].find('[s]')
                        pred = [s[:pred_EOS] for s in pred]  # prune after "end of sentence" token ([s])
                        pred_max_prob = pred_max_prob[:pred_EOS, :]
                        if opt.output_split:
                            alpha = alphas[idx, :, :].transpose()
                            img = Image.open(img_name).convert('RGB')
                            width, height = img.size
                            alpha = alpha[:pred_EOS]
                            if len(alpha) > 0:
                                last_alpha_line = alpha[-1]
                                # 消除padding的影响
                                seq_length = last_alpha_line.shape[0]
                                column_range = np.arange(0, seq_length)
                                ratio = height / width
                                # too long, compress into opt shape, don't need pad
                                if ratio > opt.imgH / opt.imgW:
                                    want_height = opt.imgW * ratio
                                    compress_ratio = want_height / opt.imgH
                                    expect_last_column = seq_length
                                # need pad
                                else:
                                    compress_ratio = 1
                                    expect_height = height / width * opt.imgW
                                    expect_last_column = expect_height / opt.imgH * seq_length
                                column_range = column_range - seq_length / 2
                                column_range = column_range / 320 * (320 + (compress_ratio - 1) * 32)
                                column_range = column_range + seq_length / 2
                                # column_range = column_range - column_range[0]
                                # last_column = np.argmax(last_alpha_line)
                                last_column = np.dot(last_alpha_line, column_range)
                                expect_linein = expect_last_column - last_column
                                split_output = os.path.join('output',
                                                            os.path.splitext(os.path.basename(img_name))[0] + '.txt')
                                with open(split_output, 'w', encoding='utf-8') as fp:
                                    draw = ImageDraw.Draw(img)
                                    for alpha_line in alpha:
                                        column = np.dot(alpha_line, column_range)
                                        line_height = int(
                                            (column - expect_linein / 2) / (last_column - expect_linein / 2) * height)
                                        # line_height = int(column / last_column * height)
                                        line = [0, line_height, width - 1, line_height]
                                        line = list(map(str, line))
                                        fp.write(','.join(line) + '\n')
                                        draw.line(((0, line_height), (width - 1, line_height)), fill=(255, 0, 0),
                                                  width=2)
                                    img.save(os.path.join('output', os.path.basename(img_name)))

                        best_pred = pred[0]
                        best_prob = pred_max_prob[:, 0]

                        # calculate confidence score (= multiply of pred_max_prob)
                        try:
                            confidence_score = best_prob.cumprod(dim=0)[-1]
                        except IndexError:
                            confidence_score = 0.0
                            # print(f'{img_name:25s}\t{pred:25s}\t can\'t predict')
                            # raise ValueError()
                        print(f'{img_name:25s}\t{best_pred:25s}\t{confidence_score:0.4f}')
                        log.write(f'{img_name:25s}\t{best_pred:25s}\t{confidence_score:0.4f}\n')
                        for i in range(k):
                            print(f'Candidatae {i:1d}: ', end='')
                            for j in range(pred_EOS):
                                print(f'{pred[i][j]}, prob: {pred_max_prob[j][i]:0.4f}\t', end='')
                            print()

                else:
                    preds_prob = F.softmax(preds, dim=2)
                    preds_max_prob, _ = preds_prob.max(dim=2)
                    for img_name, pred, pred_max_prob, pred_idx in zip(image_path_list, preds_str, preds_max_prob,
                                                                       preds_index):
                        pred_EOS = len(pred)
                        pred_max_prob = pred_max_prob[:pred_EOS]
                        # calculate confidence score (= multiply of pred_max_prob)
                        try:
                            confidence_score = pred_max_prob.cumprod(dim=0)[-1]
                        except IndexError:
                            confidence_score = 0.0
                            # print(f'{img_name:25s}\t{pred:25s}\t can\'t predict')
                            # raise ValueError()
                        if opt.output_split:
                            img = Image.open(img_name).convert('RGB')
                            width, height = img.size
                            pred_idx = pred_idx.detach().cpu().numpy().tolist()
                            preds_len = len(pred_idx)
                            ratio = height / width
                            # too long, compress into opt shape, don't need pad
                            if ratio > opt.imgH / opt.imgW:
                                want_height = opt.imgW * ratio
                                compress_ratio = want_height / opt.imgH
                                expect_last_column = preds_len
                            # need pad
                            else:
                                compress_ratio = 1
                                expect_height = height / width * opt.imgW
                                expect_last_column = expect_height / opt.imgH * preds_len
                            split_output = os.path.join('output',
                                                        os.path.splitext(os.path.basename(img_name))[0] + '.txt')

                            # hyper-parameter, suggestion 6-0.46-0.21 for 320CTC
                            # TODO find hyper-parameter for 480CTC
                            CTC_start = 6
                            center_ratio = 0.46
                            zoom_ratio = 0.21
                            # for CTC_start in np.arange(6.0, 7.1, 0.1):
                            #     for center_ratio in np.arange(0.37, 0.46, 0.01):
                            #         for zoom_ratio in np.arange(0.18, 0.23, 0.01):
                            img = Image.open(img_name).convert('RGB')
                            with open(split_output, 'w', encoding='utf-8') as fp:
                                cur_pos = 0
                                draw = ImageDraw.Draw(img)
                                index_group = itertools.groupby(pred_idx)
                                for key, group in index_group:
                                    group = list(group)
                                    if key != 0:
                                        nxt_pos = cur_pos - 1 + len(group)
                                        column = (cur_pos + nxt_pos) // 2
                                        column = column - CTC_start
                                        column = (column - preds_len * center_ratio) * (1 + zoom_ratio * compress_ratio) \
                                                 + (preds_len * center_ratio)
                                        line_height = int(column / expect_last_column * height)

                                        line = [0, line_height, width - 1, line_height]
                                        line = list(map(str, line))
                                        fp.write(','.join(line) + '\n')
                                        draw.line(((0, line_height), (width - 1, line_height)), fill=(255, 0, 0),
                                                  width=2)
                                    cur_pos += len(group)
                                img.save(os.path.join('output', os.path.basename(img_name)))
                                # img.save(os.path.join('output', '{}_{:02d}_{:03d}_{:03d}.jpg'.format(os.path.splitext(os.path.basename(img_name))[0], int(CTC_start*10), int(center_ratio*100), int(zoom_ratio*100))))

                        print(f'{img_name:25s}\t{pred:25s}\t{confidence_score:0.4f}')
                        log.write(f'{img_name:25s}\t{pred:25s}\t{confidence_score:0.4f}\n')
                log.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--image_folder', required=True, help='path to image_folder which contains text images')
    parser.add_argument('--workers', type=int, help='number of data loading workers', default=4)
    parser.add_argument('--batch_size', type=int, default=192, help='input batch size')
    parser.add_argument('--saved_model', required=True, help="path to saved_model to evaluation")
    parser.add_argument('--devices', type=str, default=None, help='CUDA devices')
    """ Data processing """
    parser.add_argument('--batch_max_length', type=int, default=25, help='maximum-label-length')
    parser.add_argument('--imgH', type=int, default=32, help='the height of the input image')
    parser.add_argument('--imgW', type=int, default=100, help='the width of the input image')
    parser.add_argument('--rgb', action='store_true', help='use rgb input')
    parser.add_argument('--character', type=str, default='0123456789abcdefghijklmnopqrstuvwxyz', help='character label')
    parser.add_argument('--sensitive', action='store_true', help='for sensitive character mode')
    parser.add_argument('--PAD', action='store_true', help='whether to keep ratio then pad for image resize')
    """ Model Architecture """
    parser.add_argument('--Transformation', type=str, required=True, help='Transformation stage. None|TPS')
    parser.add_argument('--FeatureExtraction', type=str, required=True, help='FeatureExtraction stage. VGG|RCNN|ResNet')
    parser.add_argument('--SequenceModeling', type=str, required=True, help='SequenceModeling stage. None|BiLSTM')
    parser.add_argument('--Prediction', type=str, required=True, help='Prediction stage. CTC|Attn')
    parser.add_argument('--num_fiducial', type=int, default=20, help='number of fiducial points of TPS-STN')
    parser.add_argument('--input_channel', type=int, default=1, help='the number of input channel of Feature extractor')
    parser.add_argument('--output_channel', type=int, default=512,
                        help='the number of output channel of Feature extractor')
    parser.add_argument('--hidden_size', type=int, default=256, help='the size of the LSTM hidden state')
    parser.add_argument('--page_orient', type=str, choices=['horizontal', 'vertical', 'single'], default='horizontal',
                        help='page orientation, or single char')
    parser.add_argument('--output_split', action='store_true')

    """ Output Setting """
    parser.add_argument('--topk', type=int, default=1, help='Top-k to output when single char ocr')

    opt = parser.parse_args()

    if opt.devices is not None:
        os.environ['CUDA_VISIBLE_DEVICES'] = opt.devices
        print(f'Use CUDA devices: {opt.devices}')
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    """ vocab / character number configuration """
    if opt.character in ['CN-s', 'CN-m', 'CN-l', 'CN-xl']:
        size = opt.character.split('-')[-1]
        with open('charset/charset_' + size + '.txt', 'r', encoding='utf-8') as chars:
            charset = [c.strip() for c in chars]
        charset = ''.join(charset)
        opt.character = charset
    elif opt.sensitive:
        # opt.character += 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
        opt.character = string.printable[:-6]  # same with ASTER setting (use 94 char).
    else:
        raise ValueError

    cudnn.benchmark = True
    cudnn.deterministic = True
    opt.num_gpu = torch.cuda.device_count()

    if opt.output_split:
        os.makedirs('output', exist_ok=True)

    demo(opt)
