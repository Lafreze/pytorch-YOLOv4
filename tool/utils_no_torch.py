import sys
import os
import time
import math
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from torch.autograd import Variable

import itertools
import struct  # get_image_size
import imghdr  # get_image_size


def sigmoid(x):
    return 1.0 / (np.exp(-x) + 1.)


def softmax(x):
    x = np.exp(x - np.expand_dims(np.max(x, axis=1), axis=1))
    x = x / np.expand_dims(x.sum(axis=1), axis=1)
    return x


def bbox_iou(box1, box2, x1y1x2y2=True):
    
    # print('iou box1:', box1)
    # print('iou box2:', box2)

    if x1y1x2y2:
        mx = min(box1[0], box2[0])
        Mx = max(box1[2], box2[2])
        my = min(box1[1], box2[1])
        My = max(box1[3], box2[3])
        w1 = box1[2] - box1[0]
        h1 = box1[3] - box1[1]
        w2 = box2[2] - box2[0]
        h2 = box2[3] - box2[1]
    else:
        mx = min(box1[0] - box1[2] / 2.0, box2[0] - box2[2] / 2.0)
        Mx = max(box1[0] + box1[2] / 2.0, box2[0] + box2[2] / 2.0)
        my = min(box1[1] - box1[3] / 2.0, box2[1] - box2[3] / 2.0)
        My = max(box1[1] + box1[3] / 2.0, box2[1] + box2[3] / 2.0)
        w1 = box1[2]
        h1 = box1[3]
        w2 = box2[2]
        h2 = box2[3]
    uw = Mx - mx
    uh = My - my
    cw = w1 + w2 - uw
    ch = h1 + h2 - uh
    carea = 0
    if cw <= 0 or ch <= 0:
        return 0.0

    area1 = w1 * h1
    area2 = w2 * h2
    carea = cw * ch
    uarea = area1 + area2 - carea
    return carea / uarea



def nms(boxes, nms_thresh):
    if len(boxes) == 0:
        return boxes

    det_confs = np.zeros(len(boxes))
    for i in range(len(boxes)):
        det_confs[i] = 1 - boxes[i][4]

    sortIds = np.argsort(det_confs)
    out_boxes = []
    for i in range(len(boxes)):
        box_i = boxes[sortIds[i]]
        if box_i[4] > 0:
            out_boxes.append(box_i)
            for j in range(i + 1, len(boxes)):
                box_j = boxes[sortIds[j]]
                if bbox_iou(box_i, box_j, x1y1x2y2=False) > nms_thresh:
                    # print(box_i, box_j, bbox_iou(box_i, box_j, x1y1x2y2=False))
                    box_j[4] = 0
    return out_boxes


def yolo_forward(output, conf_thresh, num_classes, anchors, num_anchors, only_objectness=1,
                              validation=False):
    # Output would be invalid if it does not satisfy this assert
    # assert (output.size(1) == (5 + num_classes) * num_anchors)

    # print(output.size())

    # Slice the second dimension (channel) of output into:
    # [ 1, 1, 1, 1, 1, num_classes, 1, 1, 1, 1, 1, num_classes, 1, 1, 1, 1, 1, num_classes ]
    #
    list_of_slices = []

    for i in range(num_anchors):
        begin = i * (5 + num_classes)
        end = (i + 1) * (5 + num_classes)
        
        for j in range(5):
            list_of_slices.append(output[:, begin + j : begin + j + 1])

        list_of_slices.append(output[:, begin + 5 : end])

    # Apply sigmoid(), exp() and softmax() to slices
    # [ 1, 1,     1, 1,     1,      num_classes, 1, 1, 1, 1, 1, num_classes, 1, 1, 1, 1, 1, num_classes ]
    #   sigmid()  exp()  sigmoid()  softmax()
    for i in range(num_anchors):
        begin = i * (5 + 1)

        # print(list_of_slices[begin].size())
        
        list_of_slices[begin] = torch.sigmoid(list_of_slices[begin])
        list_of_slices[begin + 1] = torch.sigmoid(list_of_slices[begin + 1])
        
        list_of_slices[begin + 2] = torch.exp(list_of_slices[begin + 2])
        list_of_slices[begin + 3] = torch.exp(list_of_slices[begin + 3])

        list_of_slices[begin + 4] = torch.sigmoid(list_of_slices[begin + 4])

        list_of_slices[begin + 5] = torch.nn.Softmax(dim=1)(list_of_slices[begin + 5])

    # Prepare C-x, C-y, P-w, P-h (None of them are torch related)
    batch = output.size(0)
    H = output.size(2)
    W = output.size(3)
    grid_x = np.expand_dims(np.expand_dims(np.expand_dims(np.linspace(0, W - 1, W), axis=0).repeat(H, 0), axis=0), axis=0)
    grid_y = np.expand_dims(np.expand_dims(np.expand_dims(np.linspace(0, H - 1, H), axis=1).repeat(W, 1), axis=0), axis=0)

    anchor_w = []
    anchor_h = []
    for i in range(num_anchors):
        anchor_w.append(anchors[i * 2])
        anchor_h.append(anchors[i * 2 + 1])

    device = None
    cuda_check = output.is_cuda
    if cuda_check:
        device = output.get_device()

    # Apply C-x, C-y, P-w, P-h to slices
    for i in range(num_anchors):
        begin = i * (5 + 1)
        
        list_of_slices[begin] += torch.tensor(grid_x, device=device)
        list_of_slices[begin + 1] += torch.tensor(grid_y, device=device)
        
        list_of_slices[begin + 2] *= anchor_w[i]
        list_of_slices[begin + 3] *= anchor_h[i]


    ########################################
    #   Figure out bboxes from slices     #
    ########################################

    xmin_list = []
    ymin_list = []
    xmax_list = []
    ymax_list = []

    det_confs_list = []
    cls_confs_list = []

    for i in range(num_anchors):
        begin = i * (5 + 1)

        xmin_list.append(list_of_slices[begin])
        ymin_list.append(list_of_slices[begin + 1])
        xmax_list.append(list_of_slices[begin] + list_of_slices[begin + 2])
        ymax_list.append(list_of_slices[begin + 1] + list_of_slices[begin + 3])

        # Shape: [batch, 1, H, W]
        det_confs = list_of_slices[begin + 4]

        # Shape: [batch, num_classes, H, W]
        cls_confs = list_of_slices[begin + 5]

        det_confs_list.append(det_confs)
        cls_confs_list.append(cls_confs)
    
    # Shape: [batch, num_anchors, H, W]
    xmin = torch.cat(xmin_list, dim=1)
    ymin = torch.cat(ymin_list, dim=1)
    xmax = torch.cat(xmax_list, dim=1)
    ymax = torch.cat(ymax_list, dim=1)

    # normalize coordinates to [0, 1]
    xmin = xmin / W
    ymin = ymin / H
    xmax = xmax / W
    ymax = ymax / H

    # Shape: [batch, num_anchors * H * W] 
    det_confs = torch.cat(det_confs_list, dim=1).view(batch, num_anchors * H * W)

    # Shape: [batch, num_anchors, num_classes, H * W] 
    cls_confs = torch.cat(cls_confs_list, dim=1).view(batch, num_anchors, num_classes, H * W)
    # Shape: [batch, num_anchors * H * W, num_classes] 
    cls_confs = cls_confs.permute(0, 1, 3, 2).reshape(batch, num_anchors * H * W, num_classes)

    # Shape: [batch, num_anchors * H * W, 1]
    xmin = xmin.view(batch, num_anchors * H * W, 1)
    ymin = ymin.view(batch, num_anchors * H * W, 1)
    xmax = xmax.view(batch, num_anchors * H * W, 1)
    ymax = ymax.view(batch, num_anchors * H * W, 1)

    # Shape: [batch, num_anchors * h * w, 4]
    boxes = torch.cat((xmin, ymin, xmax, ymax), dim=2).clamp(-10.0, 10.0)

    # Shape: [batch, num_anchors * h * w, num_classes, 4]
    # boxes = boxes.view(N, num_anchors * H * W, 1, 4).expand(N, num_anchors * H * W, num_classes, 4)
    

    return  boxes, cls_confs, det_confs




def get_region_boxes(boxes, cls_confs, det_confs, conf_thresh):
    
    ########################################
    #   Figure out bboxes from slices     #
    ########################################

    # boxes = np.mean(boxes, axis=2, keepdims=False)

    t2 = time.time()
    all_boxes = []
    for b in range(boxes.shape[0]):
        l_boxes = []
        for i in range(boxes.shape[1]):
            
            det_conf = det_confs[b, i]
            max_cls_conf = cls_confs[b, i].max(axis=0)
            max_cls_id= cls_confs[b, i].argmax(axis=0)

            if det_conf > conf_thresh:
                bcx = boxes[b, i, 0]
                bcy = boxes[b, i, 1]
                bw = boxes[b, i, 2] - boxes[b, i, 0]
                bh = boxes[b, i, 3] - boxes[b, i, 1]

                l_box = [bcx, bcy, bw, bh, det_conf, max_cls_conf, max_cls_id]

                l_boxes.append(l_box)
        all_boxes.append(l_boxes)
    t3 = time.time()
    if False:
        print('---------------------------------')
        print('matrix computation : %f' % (t1 - t0))
        print('        gpu to cpu : %f' % (t2 - t1))
        print('      boxes filter : %f' % (t3 - t2))
        print('---------------------------------')
    
    
    return all_boxes



def plot_boxes_cv2(img, boxes, savename=None, class_names=None, color=None):
    import cv2
    colors = torch.FloatTensor([[1, 0, 1], [0, 0, 1], [0, 1, 1], [0, 1, 0], [1, 1, 0], [1, 0, 0]]);

    def get_color(c, x, max_val):
        ratio = float(x) / max_val * 5
        i = int(math.floor(ratio))
        j = int(math.ceil(ratio))
        ratio = ratio - i
        r = (1 - ratio) * colors[i][c] + ratio * colors[j][c]
        return int(r * 255)

    width = img.shape[1]
    height = img.shape[0]
    for i in range(len(boxes)):
        box = boxes[i]
        x1 = int((box[0] - box[2] / 2.0) * width)
        y1 = int((box[1] - box[3] / 2.0) * height)
        x2 = int((box[0] + box[2] / 2.0) * width)
        y2 = int((box[1] + box[3] / 2.0) * height)

        if color:
            rgb = color
        else:
            rgb = (255, 0, 0)
        if len(box) >= 7 and class_names:
            cls_conf = box[5]
            cls_id = box[6]
            print('%s: %f' % (class_names[cls_id], cls_conf))
            classes = len(class_names)
            offset = cls_id * 123457 % classes
            red = get_color(2, offset, classes)
            green = get_color(1, offset, classes)
            blue = get_color(0, offset, classes)
            if color is None:
                rgb = (red, green, blue)
            img = cv2.putText(img, class_names[cls_id], (x1, y1), cv2.FONT_HERSHEY_SIMPLEX, 1.2, rgb, 1)
        img = cv2.rectangle(img, (x1, y1), (x2, y2), rgb, 1)
    if savename:
        print("save plot results to %s" % savename)
        cv2.imwrite(savename, img)
    return img


def plot_boxes(img, boxes, savename=None, class_names=None):
    colors = torch.FloatTensor([[1, 0, 1], [0, 0, 1], [0, 1, 1], [0, 1, 0], [1, 1, 0], [1, 0, 0]]);

    def get_color(c, x, max_val):
        ratio = float(x) / max_val * 5
        i = int(math.floor(ratio))
        j = int(math.ceil(ratio))
        ratio = ratio - i
        r = (1 - ratio) * colors[i][c] + ratio * colors[j][c]
        return int(r * 255)

    width = img.width
    height = img.height
    draw = ImageDraw.Draw(img)
    for i in range(len(boxes)):
        box = boxes[i]
        x1 = (box[0] - box[2] / 2.0) * width
        y1 = (box[1] - box[3] / 2.0) * height
        x2 = (box[0] + box[2] / 2.0) * width
        y2 = (box[1] + box[3] / 2.0) * height

        rgb = (255, 0, 0)
        if len(box) >= 7 and class_names:
            cls_conf = box[5]
            cls_id = box[6]
            print('%s: %f' % (class_names[cls_id], cls_conf))
            classes = len(class_names)
            offset = cls_id * 123457 % classes
            red = get_color(2, offset, classes)
            green = get_color(1, offset, classes)
            blue = get_color(0, offset, classes)
            rgb = (red, green, blue)
            draw.text((x1, y1), class_names[cls_id], fill=rgb)
        draw.rectangle([x1, y1, x2, y2], outline=rgb)
    if savename:
        print("save plot results to %s" % savename)
        img.save(savename)
    return img


def read_truths(lab_path):
    if not os.path.exists(lab_path):
        return np.array([])
    if os.path.getsize(lab_path):
        truths = np.loadtxt(lab_path)
        truths = truths.reshape(truths.size / 5, 5)  # to avoid single truth problem
        return truths
    else:
        return np.array([])


def load_class_names(namesfile):
    class_names = []
    with open(namesfile, 'r') as fp:
        lines = fp.readlines()
    for line in lines:
        line = line.rstrip()
        class_names.append(line)
    return class_names


def do_detect(model, img, conf_thresh, n_classes, nms_thresh, use_cuda=1):
    model.eval()
    t0 = time.time()

    if isinstance(img, Image.Image):
        width = img.width
        height = img.height
        img = torch.ByteTensor(torch.ByteStorage.from_buffer(img.tobytes()))
        img = img.view(height, width, 3).transpose(0, 1).transpose(0, 2).contiguous()
        img = img.view(1, 3, height, width)
        img = img.float().div(255.0)
    elif type(img) == np.ndarray and len(img.shape) == 3:  # cv2 image
        img = torch.from_numpy(img.transpose(2, 0, 1)).float().div(255.0).unsqueeze(0)
    elif type(img) == np.ndarray and len(img.shape) == 4:
        img = torch.from_numpy(img.transpose(0, 3, 1, 2)).float().div(255.0)
    else:
        print("unknow image type")
        exit(-1)

    t1 = time.time()

    if use_cuda:
        img = img.cuda()
    img = torch.autograd.Variable(img)
    t2 = time.time()

    boxes_and_confs = model(img)

    # print(boxes_and_confs)
    output = []
    
    for i in range(len(boxes_and_confs)):
        output.append([])
        output[-1].append(boxes_and_confs[i][0].cpu().detach().numpy())
        output[-1].append(boxes_and_confs[i][1].cpu().detach().numpy())
        output[-1].append(boxes_and_confs[i][2].cpu().detach().numpy())

    '''
    for i in range(len(boxes_and_confs)):
        output.append(boxes_and_confs[i].cpu().detach().numpy())
    '''

    return post_processing(img, conf_thresh, n_classes, nms_thresh, output)


def post_processing(img, conf_thresh, n_classes, nms_thresh, output):

    anchors = [12, 16, 19, 36, 40, 28, 36, 75, 76, 55, 72, 146, 142, 110, 192, 243, 459, 401]
    num_anchors = 9
    anchor_masks = [[0, 1, 2], [3, 4, 5], [6, 7, 8]]
    strides = [8, 16, 32]
    anchor_step = len(anchors) // num_anchors

    boxes = []  
    
    for i in range(len(output)):
        boxes.append(get_region_boxes(output[i][0], output[i][1], output[i][2], conf_thresh))
    '''
    for i in range(3):
        masked_anchors = []
        for m in anchor_masks[i]:
            masked_anchors += anchors[m * anchor_step:(m + 1) * anchor_step]
        masked_anchors = [anchor / strides[i] for anchor in masked_anchors]
        boxes.append(get_region_boxes_out_model(output[i], conf_thresh, 80, masked_anchors, len(anchor_masks[i])))
    '''
    if img.shape[0] > 1:
        bboxs_for_imgs = [
            boxes[0][index] + boxes[1][index] + boxes[2][index]
            for index in range(img.shape[0])]
        # 分别对每一张图片的结果进行nms
        t3 = time.time()
        boxes = [nms(bboxs, nms_thresh) for bboxs in bboxs_for_imgs]
    else:
        boxes = boxes[0][0] + boxes[1][0] + boxes[2][0]
        t3 = time.time()
        boxes = nms(boxes, nms_thresh)
    t4 = time.time()

    if False:
        print('-----------------------------------')
        print(' image to tensor : %f' % (t1 - t0))
        print('  tensor to cuda : %f' % (t2 - t1))
        print('         predict : %f' % (t3 - t2))
        print('             nms : %f' % (t4 - t3))
        print('           total : %f' % (t4 - t0))
        print('-----------------------------------')
    return boxes
