from __future__ import print_function

import torch
import numpy as np
import pycuda.driver as cuda
import pycuda.autoinit

import sys, os
sys.path.insert(1, os.path.join(sys.path[0], ".."))
import common
import tensorrt as trt
import pycuda.driver as cuda
import pycuda.autoinit
from PIL import ImageDraw


import cv2
import common
import time
import calibrator
from torchvision import transforms
import torch.utils.data as data
from PIL import Image
from torch.utils.data import dataloader
from base_module import BaseModule
from util import *
from alpha_yolo3_module_drawing import drawing

TRT_LOGGER = trt.Logger()

def get_engine(onnx_file_path, engine_file_path,calib):
    """Attempts to load a serialized engine if available, otherwise builds a new TensorRT engine and saves it."""
    def build_engine():
        """Takes an ONNX file and creates a TensorRT engine to run inference with"""
        with trt.Builder(TRT_LOGGER) as builder, builder.create_network() as network, trt.OnnxParser(network, TRT_LOGGER) as parser:
            builder.max_workspace_size = 1 << 30  # 1GB
            builder.max_batch_size = 1
            builder.int8_mode = True
            builder.int8_calibrator = calib
            # Parse model file
            if not os.path.exists(onnx_file_path):
                print('ONNX file {} not found, please run yolov3_to_onnx.py first to generate it.'.format(onnx_file_path))
                exit(0)
            print('Loading ONNX file from path {}...'.format(onnx_file_path))
            with open(onnx_file_path, 'rb') as model:
                print('Beginning ONNX file parsing')
                parser.parse(model.read())
            print('Completed parsing of ONNX file')
            print('Building an engine from file {}; this may take a while...'.format(onnx_file_path))
            engine = builder.build_cuda_engine(network)
            print("Completed creating Engine")
            with open(engine_file_path, "wb") as f:
                f.write(engine.serialize())
            # return engine

    if os.path.exists(engine_file_path):
        if os.path.exists(engine_file_path):
            print("Reading engine from file {}".format(engine_file_path))
            with open(engine_file_path, "rb") as f, trt.Runtime(TRT_LOGGER) as runtime:
                return runtime.deserialize_cuda_engine(f.read())
    else:
        build_engine()

class MyDataset(data.Dataset):
    def __init__(self, images):
        self.images = images
        self.transform = transforms.Compose([
            transforms.Resize((416, 416), interpolation=3),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
        ])

    def __getitem__(self, index):
        return self.transform(self.images)

    def __len__(self):
        return 1
def load_img(image):
    dataset = MyDataset(image)
    loader = dataloader.DataLoader(dataset, 1, num_workers=1)
    return loader


def prep_image(orig_im, inp_dim):
    dim = orig_im.shape[1], orig_im.shape[0]
    img = (letterbox_image(orig_im, (inp_dim, inp_dim)))
    img_ = img[:, :, ::-1].transpose((2, 0, 1)).copy() #(3 608 608)
    img_ = torch.from_numpy(img_).float().div(255.0).unsqueeze(0)
    img_ = img_.numpy()
    return img_, orig_im, dim

def letterbox_image(img, inp_dim):
    '''resize image with unchanged aspect ratio using padding'''
    img_w, img_h = img.shape[1], img.shape[0]
    w, h = inp_dim
    new_w = int(img_w * min(w / img_w, h / img_h))
    new_h = int(img_h * min(w / img_w, h / img_h))
    resized_image = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_CUBIC)
    canvas = np.full((inp_dim[1], inp_dim[0], 3), 128)
    canvas[(h - new_h) // 2:(h - new_h) // 2 + new_h, (w - new_w) // 2:(w - new_w) // 2 + new_w, :] = resized_image
    return canvas

class trt_yolo3_module(BaseModule):
    def __init__(self, init_dict):
        a = torch.cuda.FloatTensor()
        builder = trt.Builder(TRT_LOGGER)
        #builder.fp16_mode = True
        builder.int8_mode = True
        builder.strict_type_constraints = True
        self.trt_file = init_dict['trt']
        self.use_cuda = init_dict['use_cuda']
        self.inp_dim = 416
        self.num_classes = 80
        self.output_shapes = [(1, 255, 13, 13), (1, 255, 26, 26), (1, 255, 52, 52)] #yolo3-416
        self.yolo_anchors = [[(116, 90), (156, 198), (373, 326)],
                             [(30, 61),  (62, 45),   (59, 119)],
                             [(10, 13),  (16, 30),   (33, 23)]]

        self.engine = get_engine(self.trt_file,engine_file_path,calib)
        self.inputs, self.outputs, self.bindings, self.stream = common.allocate_buffers(self.engine)
        self.context = self.engine.create_execution_context()

    def preparing(self,orig_img_list):
        img = []
        orig_img = []
        im_name = []
        im_dim_list = []
        batch = 1
        for im in orig_img_list:
            im_name_k = ''
            img_k, orig_img_k, im_dim_list_k = prep_image(im, self.inp_dim)
            img.append(img_k)
            orig_img.append(orig_img_k)
            im_name.append(im_name_k)
            im_dim_list.append(im_dim_list_k)

        with torch.no_grad():
            im_dim_list = torch.FloatTensor(im_dim_list).repeat(1,2)
            im_dim_list_ = im_dim_list

        procession_tuple = (img, orig_img, im_name, im_dim_list)
        return procession_tuple

    def detection(self,procession_tuple):
        (img, orig_img, im_name, im_dim_list) = procession_tuple
        # with get_engine(self.trt_file) as engine, engine.create_execution_context() as context:
        if 1:
            # inputs, outputs, bindings, stream = common.allocate_buffers(self.engine)
            inference_start = time.time()
            self.inputs[0].host = img[0] #waiting fix bug
            trt_outputs = common.do_inference(self.context, bindings=self.bindings, inputs=self.inputs, outputs=self.outputs, stream=self.stream)
            inference_end = time.time()
            # print('inference time : %f' % (inference_end-inference_start))
            write = 0
            for output, shape, anchors in zip(trt_outputs, self.output_shapes, self.yolo_anchors):
                output = output.reshape(shape)
                trt_output = torch.from_numpy(output).cuda().data
                # trt_output = trt_output.dataq
                # cuda_time1 = time.time()
                trt_output = predict_transform(trt_output, self.inp_dim, anchors, self.num_classes, self.use_cuda)
                # cuda_time2 = time.time()
                # print('CUDA time : %f' % (cuda_time2 - cuda_time1))
                if type(trt_output) == int:
                    continue

                if not write:
                    detections = trt_output
                    write = 1

                else:
                    detections = torch.cat((detections, trt_output), 1)

            o_time1 = time.time()
            print('TensorRT inference time : %f' % (o_time1-inference_start))
            dets = dynamic_write_results(detections, 0.8, self.num_classes, nms=True, nms_conf=0.45)
            o_time2 = time.time()
            print('After process time : %f' %(o_time2-o_time1))

            class_list_all = []
            box_list_all = []
            conf_list_all = []
            if not isinstance(dets,int):
                dets = dets.cpu()
                im_dim_list = torch.index_select(im_dim_list,0, dets[:, 0].long())
                scaling_factor = torch.min(self.inp_dim / im_dim_list, 1)[0].view(-1, 1)
                dets[:, [1, 3]] -= (self.inp_dim - scaling_factor * im_dim_list[:, 0].view(-1, 1)) / 2
                dets[:, [2, 4]] -= (self.inp_dim - scaling_factor * im_dim_list[:, 1].view(-1, 1)) / 2
                dets[:, 1:5] /= scaling_factor
                for j in range(dets.shape[0]):
                    dets[j, [1, 3]] = torch.clamp(dets[j, [1, 3]], 0.0, im_dim_list[j, 0])
                    dets[j, [2, 4]] = torch.clamp(dets[j, [2, 4]], 0.0, im_dim_list[j, 1])
                boxes = dets[:, 1:5]
                scores = dets[:, 5:6]
                for k in range(len(orig_img)):
                    boxes_k = boxes[dets[:,0]==k]
                    scores_k = scores[dets[:,0]==k]
                    class_list = []
                    box_list = []
                    for b in boxes_k:
                        x1=int(b[0])
                        x2=int(b[2])
                        y1=int(b[1])
                        y2=int(b[3])
                        box_list.append([x1,x2,y1,y2])
                        class_list.append('person')		

                    score_list = scores_k.numpy().tolist()
                    s_list = []
                    for s in score_list:
                        s_list.append(s[0])
                    box_list_all.append(box_list)
                    conf_list_all.append(s_list)
                    class_list_all.append(class_list)
            o_time3 = time.time()
            print('All time : %f' % (o_time3 - inference_start))

        return (class_list_all,box_list_all,conf_list_all)            



    def dict_checkup(self,dict):
        if 'img' not in dict:
            dict['img']= ''
            print('no img in dict')	
        if 'data' not in dict:
            dict['data']={}
            print('no data in dict')
        if 'info' not in dict:
            dict['info']={}
            print('no info in dict')	

    def process_frame(self, frame_dic):
        pass

    def process_frame_batch(self, frame_dic_list):
        for dic in frame_dic_list:
            self.dict_checkup(dic)
        
        img_list = []
        for dic in frame_dic_list:
            img_list.append(dic['img'])
        
        procession_tuple = self.preparing(img_list)
        # (img, orig_img, im_name, im_dim_list) = procession_tuple
        (class_list_all,box_list_all,conf_list_all) = self.detection(procession_tuple)
        if len(class_list_all) == 0:
            for frame_dic in frame_dic_list:
                frame_dic['data']['number'] = 0
                frame_dic['data']['box_list'] = []
                frame_dic['data']['class_list'] = []
                frame_dic['data']['conf_list'] = []
        else:
            for i,frame_dic in enumerate(frame_dic_list):
                frame_dic['data']['number'] = len(class_list_all[i])
                frame_dic['data']['box_list'] = box_list_all[i]
                frame_dic['data']['class_list'] = class_list_all[i]
                frame_dic['data']['conf_list'] = conf_list_all[i]

        return frame_dic_list


if __name__ == '__main__':
    """Create a TensorRT engine for ONNX-based YOLOv3 and run inference."""
    calibration_cache = "yolo_416.cache"
    calib = calibrator.ExampleEntropyCalibrator(loader=load_img(Image.open('dog.jpg')), cache_file=calibration_cache,
                                                c=3, h=416, w=416)

    # Try to load a previously generated YOLOv3-608 network graph in ONNX format:
    onnx_file_path = 'yolov3.onnx'
    engine_file_path = "yolov3-416-int8.trt"
    get_engine(onnx_file_path, engine_file_path,calib)

    init_dict = {'trt':"yolov3-416-int8.trt", 'use_cuda':True}
    alpha_yolo3_unit = trt_yolo3_module(init_dict)

    url = 'rtsp://admin:oeasy808@192.168.9.4'  # 增加网络摄像头
    #img_path = './images/person4.jpg'
    img_path="./images/PETS09-S2L1.mp4"

    video_capture = cv2.VideoCapture(img_path)
    #video_capture = cv2.VideoCapture(url)

    while True:
        input_dic_list = []
        ret, frame = video_capture.read()  # frame shape 640*480*3
        if ret != True:
            break
        dic = {'img':frame,'data':{},'info':{}}
        # img_path = './images/person4.jpg'
        # dic = {'img': cv2.imread(img_path), 'data': {}, 'info': {}}



        input_dic_list.append(dic)


        output_dic_list = alpha_yolo3_unit.process_frame_batch(input_dic_list)
        #print(output_dic_list[0])
        box = output_dic_list[0]['data']['box_list']

        #print(box)
        for dic in output_dic_list:
            img_array = dic['img']
            drawing(img_array, dic)
            #print(img_array)
            cv2.imshow('show', img_array)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break
            #cv2.waitKey(5000)

    
