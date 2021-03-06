import torch
import torch.nn as nn
import torchvision.models as models
import torchvision.transforms as transforms
from torch.autograd import Variable
from PIL import Image
import config
import cv2
import matplotlib.pyplot as plt
import numpy as np
import os
from copy import deepcopy
from collections import deque
from sklearn.metrics.pairwise import cosine_similarity 
from VLN_config import config

#device = torch.device('cude') if torch.cuda.is_available() else torch.device('cpu')
class featureExtractor ():
    def __init__ (self, image_paths, model):
        self.image_paths = image_paths
        self.model = model
        self.temporal_memory_buffer = {'features' : deque(), 'infos' : deque() } # save past buffer to track objects
        self.max_temporal_memory_buffer = config.max_temporal_memory_buffer # max images to track an object
        self.best_features = config.best_features # best features to keep
        self.threshold_similarity = config.threshold_similarity # less than this threshold we dont track any box anymore
        self.track_temporal_features = config.track_temporal_features
        self.mean_layer = config.mean_layer # so the embedding of the box has the shape of [2048] if mean_layer == False
                                    # or the embedding of the box has the shape of [max_temporal_memory_buffer, 2048] 
                                    # if mean_layer == True

    def image_transform(self, image_path):
        ''' read image from single image path, transfer it to tensor and get its 
        infromation '''
        to_tensor = transforms.ToTensor()
        #img = Image.open(pic) 
        im = cv2.imread(image_path) # Read image with cv2
        im = cv2.cvtColor(im, cv2.COLOR_BGR2RGB) # Convert to RGB
        #t_img = to_tensor(img).float() # convert to tensor
        im_shape = im.shape
        im_height = im_shape[0]
        im_width = im_shape[1]

        img = to_tensor(im).float() # convert to tensor
        im_info = {"width": im_width, "height": im_height}
        return img, im_info
    
    def add_temporal_memory_buffer (self, features, infos):
        ''' Memory buffer contains the features and infos of the last max_temporal_memory_buffer images
        so that we can track the objects and get a better representation
        '''
        if len(self.temporal_memory_buffer['features']) < self.max_temporal_memory_buffer:
            self.temporal_memory_buffer['features'].append(features)
            self.temporal_memory_buffer['infos'].append(infos)
        else:
            self.temporal_memory_buffer['features'].append(features)
            self.temporal_memory_buffer['features'].popleft()
            self.temporal_memory_buffer['infos'].append(infos)
            self.temporal_memory_buffer['infos'].popleft()
        return self.temporal_memory_buffer

    def similarity (self, roi_feat_1, roi_feat_2):
        ''' computes similarity as the normalized dot product 
        '''
        roi_feat_1 = roi_feat_1.reshape(1, -1)# because we have one sample ==> [1,2048]
        roi_feat_2 = roi_feat_2.reshape(1, -1)# because we have one sample ==> [1,2048]
        return cosine_similarity(roi_feat_1, roi_feat_2)[0][0]

    def get_best_smilar_box (self, box_embedding, other_image_boxes_embeddings):
        '''other_image_boxes_embeddings have the shape of [nb boxes, 2048]
        box_embedding have the shape of [2048]'''
        list_similarities = []
        for other_box_embedding in other_image_boxes_embeddings:
            #import pdb; pdb.set_trace()
            list_similarities.append(self.similarity(box_embedding, other_box_embedding))
        list_similarities = np.asarray(list_similarities)

        max_similarty = np.amax(list_similarities)
        max_similarity_index = list_similarities.argmax()

        similar_embedding = other_image_boxes_embeddings[max_similarity_index]
        if max_similarty < self.threshold_similarity:
            return False, similar_embedding
        else:
            return True, similar_embedding

    def get_rpn_rois (self):
        '''This function returns the embedding of fc6 layer of the selected ROIS
        shape output is [nb regions, 1024]
        input: self.im which is the current image tensor it has shape [3,w,h]
        output tensors: selected_rois, boxes_on_image, labels, scores'''
        outputs = []
        self.model.eval()
        with torch.no_grad():
            hook = self.model.backbone.register_forward_hook(
                lambda self, input, output: outputs.append(output)
                )
            res = self.model([self.im])
            hook.remove()

            #rpn_head (nn.Module): module that computes the objectness and regression deltas from the RPN
            # box_roi_pool (MultiScaleRoIAlign): the module which crops and resizes the feature maps in
                    #the locations indicated by the bounding boxes (output of RPN)
            
        
            this_output =  self.model.roi_heads.box_roi_pool(
                    outputs[0], [r['boxes'] for r in res], [i.shape[-2:] for i in self.im]
                    )
            this_output = this_output.flatten(start_dim=1)
            this_output= self.model.roi_heads.box_head.fc6(this_output)
            #self.embedding_rois.append(this_output) # if you want embedding :[nb box, 1024]
            self.embedding_rois.append(torch.cat((this_output,this_output), 1)) # if you want embedding : [nb box, 1024]

        for i in range (len(res)):
            self.boxes_on_image.append( res[i]['boxes'])
            self.labels.append( res[i]['labels'])
            self.scores.append (res[i]['scores'])
        
    def get_selected_rois (self):
        '''Input tensors: all of them are list of tensors 
        labels[0] is tensors of the labels in image 0
        Output Tensors are the same input tensors just after selecting based on the threshold'''
        for i in range(len(self.labels)): # number of images in the list
            #ind_roi = [] # list of indexes of RoIs with scores higher than threshold
            #high_indx = 1 # since the scores are sorted, we can get the higher idx that bigger thn threshold
                # at the worst case, we find one value
            #for j in range (scores[i].shape[0]):
            #    if scores[i][j].item() >= threshold :
            #        ind_roi.append(j)
            #        high_indx = j
            if len(self.embedding_rois[i]) >= self.best_features:
                self.embedding_rois[i] = self.embedding_rois[i][:self.best_features]
                self.boxes_on_image[i] = self.boxes_on_image[i][:self.best_features]
                self.labels[i] = self.labels[i][:self.best_features]
                self.scores[i] = self.scores[i][:self.best_features]

    def get_temporal_feature(self):
        for i in range (len(self.embedding_rois[-1])):
            ''' iterate over the boxes of the current image'''
            print('box_id i: ', i, 'len memory' ,len(self.temporal_memory_buffer['features']))
            curr_embedding_roi = self.embedding_rois[-1][i]# current box embedding
            #if len(curr_embedding_roi.shape) >1 : # it means that we are taking more than one image
            #    curr_embedding_roi = curr_embedding_roi[-1] # we take the last image
            all_curr_embedding_roi = curr_embedding_roi
            if self.mean_layer == True and all_curr_embedding_roi.shape[0] != self.max_temporal_memory_buffer:
                all_curr_embedding_roi = all_curr_embedding_roi.reshape(1,-1)
                all_curr_embedding_roi = torch.cat((torch.zeros(self.max_temporal_memory_buffer -1, all_curr_embedding_roi.shape[1])\
                                        , all_curr_embedding_roi), 0)
            if len(self.temporal_memory_buffer['features']) != 1:
                ## NOT FIRST IMAGE
                for j in range(len(self.temporal_memory_buffer['features']) -2 , -1 , -1):
                    # first value of j is len(tem_mem_buffer) - 2 and last one is 0
                    print('j', j)
                    ''' we iterate over the last images
                    we iterate over the indexes of self.temporal_memory_buffer because it is either equal 
                    to max_temporal_memory_buffer or less, it can not be more (verified when we add to the buffer)
                    '''
                    #if len(self.temporal_memory_buffer)< self.max_temporal_memory_buffer:
                    # if the buffer is still new

                    if len(all_curr_embedding_roi.shape) >1 : # it means that we are taking more than one image  
                        curr_embedding_roi = all_curr_embedding_roi[-1] # we take the last image
                    print('image from the past id j: ', j)
                    #print(curr_embedding_roi.shape,self.temporal_memory_buffer['features'][-j].shape )
                    # we go backward in order to get the lastest image in the buffer
                    
                    bool_similarity, similar_embedding = self.get_best_smilar_box (curr_embedding_roi, \
                                self.temporal_memory_buffer['features'][j])
                    #print(bool_similarity)
                    if bool_similarity == False:
                        if self.mean_layer == True:
                            break
                        else:
                            if len(all_curr_embedding_roi.shape) < 2:
                                all_curr_embedding_roi = all_curr_embedding_roi.reshape(1,-1)
                            break
                    else:
                        if self.mean_layer == True:
                            
                            print('########found similarity, current embedding', all_curr_embedding_roi, 'similar to: ', similar_embedding)
                            all_curr_embedding_roi[ - len(self.temporal_memory_buffer['features']) +j ] = similar_embedding.reshape(1,-1)
                            print('after cat all_curr_embedding_roi', all_curr_embedding_roi)
                        else:
                            print('########found similarity, current embedding', all_curr_embedding_roi, 'similar to: ', similar_embedding)
                            #print(curr_embedding_roi.shape )
                            if len(all_curr_embedding_roi.shape) < 2:
                                all_curr_embedding_roi = all_curr_embedding_roi.reshape(1,-1)
                            similar_embedding = similar_embedding.reshape(1,-1)
                            all_curr_embedding_roi = torch.cat((similar_embedding, all_curr_embedding_roi), 0)
                #else
                if self.mean_layer == True: 
                    #import pdb;pdb.set_trace()
                    if len(self.embedding_rois[-1].shape) < 3:
                        self.embedding_rois[-1] = self.embedding_rois[-1].unsqueeze(1) # nb_box, 1, 1024
                        torch_to_add = torch.zeros(self.embedding_rois[-1].shape[0], self.max_temporal_memory_buffer-1, self.embedding_rois[-1].shape[2]) # # nb_box, m-1, 1024
                        print('shapes 1: ', torch_to_add.shape, all_curr_embedding_roi.shape, self.embedding_rois[-1].shape)
                        self.embedding_rois[-1]= torch.cat((torch_to_add, self.embedding_rois[-1]) , 1) # nb_box, m, 1024             
                    print('shapes 3 : ', self.embedding_rois[-1][i].shape, self.embedding_rois[-1][i], all_curr_embedding_roi.shape, all_curr_embedding_roi)
                    self.embedding_rois[-1][i] = all_curr_embedding_roi
                else:
                    print('## before calculating thee mean for box id: ', i)
                    print(all_curr_embedding_roi.shape, all_curr_embedding_roi)
                    self.embedding_rois[-1][i] = torch.mean (all_curr_embedding_roi, 0)
                    print('## After calculating thee mean for box id: ', i)
                    print(self.embedding_rois[-1][i].shape, self.embedding_rois[-1][i])

            elif len(self.temporal_memory_buffer['features']) == 1:
                # DEAL WITH FIRST IMAGE
                if self.mean_layer == False:
                    pass
                else:
                    if len(self.embedding_rois[-1].shape) < 3:
                        self.embedding_rois[-1] = self.embedding_rois[-1].unsqueeze(1) # nb_box, 1, 1024
                        torch_to_add = torch.zeros(self.embedding_rois[-1].shape[0], self.max_temporal_memory_buffer-1, self.embedding_rois[-1].shape[2]) # # nb_box, m-1, 1024
                        print('shapes 1: ', torch_to_add.shape, all_curr_embedding_roi.shape, self.embedding_rois[-1].shape)
                        self.embedding_rois[-1]= torch.cat((torch_to_add, self.embedding_rois[-1]) , 1) # nb_box, m, 1024             
                    self.embedding_rois[-1][i] = all_curr_embedding_roi
                    
                    
        self.embedding_rois[-1] = self.embedding_rois[-1].view(self.embedding_rois[-1].shape[0], -1)

            
            

    def process_feature_extraction(self, output, im_infos):
        '''
        output = {
            'embedding_rois': rois,
            'bbox': boxes_on_image,
            'labels': labels,
            'scores': scores
            }
        im_info = {"width": im_width, "height": im_height}
        '''
        batch_size = len(output["embedding_rois"])
        feat_list = []
        info_list = []

        for i in range(batch_size):
            feat_list.append(output['embedding_rois'][i])
            #feat_list.append(torch.cat((output['embedding_rois'][i],output['embedding_rois'][i]), 1))
            info_list.append(
                {
                    "bbox": output['bbox'][i].cpu().numpy(),
                    "num_boxes": len(output['bbox'][i]),
                    "objects": output['labels'][i],
                    "image_width": im_infos[i]["width"],
                    "image_height": im_infos[i]["height"],
                    "cls_prob": output['scores'][i].cpu().numpy(),
                }
            )

        return feat_list, info_list


    def extract_features(self):
        '''
        image_paths: is a list of input image paths
        '''
        self.im_infos = []
        self.embedding_rois = []
        self.boxes_on_image = []
        self.labels = []
        self.scores = []
        im_nb = 0
        for image_path in self.image_paths:
            im_nb += 1
            print('#############current image: ', im_nb, '#############')
            self.im, self.im_info = self.image_transform(image_path)
            self.im_infos.append(self.im_info)
            self.get_rpn_rois ()
            self.get_selected_rois () 
            # the output here : self.embedding_rois, self.boxes_on_image, self.labels, self.scores
            #print('len self.embedding_rois: ', len(self.embedding_rois), 'shape first elt: ', self.embedding_rois[0].shape)
            # we add to the buffer the true last feature, without applying the mean or anytemporal transformation
            #print("-----Before-----> ", self.temporal_memory_buffer["features"])
            #print("-----New-----> ", self.embedding_rois[-1])
            #print("-----After------> ", self.temporal_memory_buffer["features"])
            #if len(self.temporal_memory_buffer['features'])>1:
            if self.track_temporal_features:
                cp_curr_emb = deepcopy(self.embedding_rois[-1])
                self.add_temporal_memory_buffer(cp_curr_emb, self.im_info)  
                self.get_temporal_feature()
            #print("-----After temp------> ", self.temporal_memory_buffer["features"])
            

        output = {
            'embedding_rois': self.embedding_rois,
            'bbox': self.boxes_on_image,
            'labels': self.labels,
            'scores': self.scores
            }

        features, infos = self.process_feature_extraction(
            output,
            self.im_infos,
        )

        return features, infos







def visualize_tensor(t_img_list ,boxes_on_image_tensor, labels_tensor, scores_tensor):
    ''' visualize all the images in the t_img_list
    t_img is one of the image tensors
    '''
    for im in range(len(t_img_list)):
        t_img = t_img_list[im]
        img = t_img.squeeze(0).permute(1, 2, 0) * 255
        img = img.numpy().astype(np.uint8)
        pred_class = []
        coco = config.coco['coco_instance_category_name']

        for i in list(labels_tensor[im].numpy()):
            pred_class.append(coco[i])
        
        pred_boxes = [[(i[0], i[1]), (i[2], i[3])] for i in list(boxes_on_image_tensor[im].detach().numpy())] # Bounding boxes
        pred_score = list(scores_tensor[im].detach().numpy())
        
        #for i in range(len(pred_boxes)):
        #    cv2.rectangle(img, pred_boxes[i][0], pred_boxes[i][1],color=(255, 0, 0), thickness=1) # Draw Rectangle with the coordinates
        #    cv2.putText(img,pred_class[i], pred_boxes[i][0],  cv2.FONT_HERSHEY_SIMPLEX, 1, (0,255,0),1, cv2.LINE_AA) # Write the prediction class
        #    cv2.putText(img,str(round(pred_score[i], 2)), pred_boxes[i][1],  cv2.FONT_HERSHEY_SIMPLEX, 1, (255,0,0),1, cv2.LINE_AA) # Write the prediction class

        print('labels: ', pred_class)
        print('scores: ', pred_score)
        plt.figure(figsize=(20,30)) # display the output image
        plt.imshow(img)
        plt.xticks([])
        plt.yticks([])
        plt.show()

def _chunks(self, array, chunk_size):
        for i in range(0, len(array), chunk_size):
            yield array[i : i + chunk_size]

def _save_feature(self, file_name, feature, info):
    file_base_name = os.path.basename(file_name)
    file_base_name = file_base_name.split(".")[0]
    info["image_id"] = file_base_name
    info["features"] = feature.cpu().numpy()
    file_base_name = file_base_name + ".npy"


if __name__ == '__main__':
    ## Faster RCNN 
    model = models.detection.fasterrcnn_resnet50_fpn(pretrained=True)
    # list image 
    pic = "test2.png"
    pic1 = "test.png"
    image_paths = [pic, pic1, pic1, pic1, pic1, pic1]
    f_extractor = featureExtractor(image_paths, model)
    features, infos = f_extractor.extract_features()
    import pdb; pdb.set_trace()
