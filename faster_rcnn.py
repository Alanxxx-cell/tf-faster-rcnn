import os
import tensorflow as tf
from tensorflow.keras.models import Model
from tensorflow.keras.layers import Layer, Lambda, Input, Conv2D, TimeDistributed, Dense, Flatten, BatchNormalization, Dropout
import numpy as np
import helpers
import rpn

class RoIBBox(Layer):
    """Generating bounding boxes from rpn predictions.
    First calculating the boxes from predicted deltas and label probs.
    Then applied non max suppression and selecting "nms_topn" boxes.
    Finally selecting "total_pos_bboxes" boxes using the IoU values.
    This process mostly same with the selecting bounding boxes for rpn.
    inputs:
        rpn_bbox_deltas = (batch_size, img_output_height, img_output_width, anchor_count * [delta_y, delta_x, delta_h, delta_w])
            img_output_height and img_output_width are calculated to the base model output
            they are (img_height // stride) and (img_width // stride) for VGG16 backbone
        rpn_labels = (batch_size, img_output_height, img_output_width, anchor_count)
        anchors = (batch_size, img_output_height * img_output_width * anchor_count, [y1, x1, y2, x2])
        gt_boxes = (batch_size, padded_gt_boxes_size, [y1, x1, y2, x2])
            there are could be -1 values because of the padding

    outputs:
        roi_bboxes = (batch_size, total_pos_bboxes + total_neg_bboxes, [y1, x1, y2, x2])
        gt_box_indices = (batch_size, total_pos_bboxes)
            holds the index value of the ground truth bounding box for each selected positive roi box
    """

    def __init__(self, hyper_params, **kwargs):
        super(RoIBBox, self).__init__(**kwargs)
        self.hyper_params = hyper_params

    def get_config(self):
        config = super(RoIBBox, self).get_config()
        config.update({"hyper_params": self.hyper_params})
        return config

    def call(self, inputs):
        rpn_bbox_deltas = inputs[0]
        rpn_labels = inputs[1]
        anchors = inputs[2]
        gt_boxes = inputs[3]
        #
        total_pos_bboxes = self.hyper_params["total_pos_bboxes"]
        total_neg_bboxes = self.hyper_params["total_neg_bboxes"]
        total_bboxes = total_pos_bboxes + total_neg_bboxes
        anchors_shape = tf.shape(anchors)
        batch_size, total_anchors = anchors_shape[0], anchors_shape[1]
        rpn_bbox_deltas = tf.reshape(rpn_bbox_deltas, (batch_size, total_anchors, 4))
        rpn_labels = tf.reshape(rpn_labels, (batch_size, total_anchors, 1))
        #
        rpn_bboxes = helpers.get_bboxes_from_deltas(anchors, rpn_bbox_deltas)
        rpn_bboxes = tf.reshape(rpn_bboxes, (batch_size, total_anchors, 1, 4))
        nms_bboxes, _, _, _ = helpers.non_max_suppression(rpn_bboxes, rpn_labels,
                                                          max_output_size_per_class=self.hyper_params["nms_topn"],
                                                          max_total_size=self.hyper_params["nms_topn"])
        ################################################################################################################
        pos_bbox_indices, neg_bbox_indices, gt_box_indices = helpers.get_selected_indices(nms_bboxes, gt_boxes, total_pos_bboxes, total_neg_bboxes)
        #
        pos_roi_bboxes = tf.gather(nms_bboxes, pos_bbox_indices, batch_dims=1)
        neg_roi_bboxes = tf.zeros((batch_size, total_neg_bboxes, 4), tf.float32)
        roi_bboxes = tf.concat([pos_roi_bboxes, neg_roi_bboxes], axis=1)
        return tf.stop_gradient(roi_bboxes), tf.stop_gradient(gt_box_indices)

class RoIDelta(Layer):
    """Calculating faster rcnn actual bounding box deltas and labels.
    This layer only running on the training phase.
    inputs:
        roi_bboxes = (batch_size, total_pos_bboxes + total_neg_bboxes, [y1, x1, y2, x2])
        gt_boxes = (batch_size, padded_gt_boxes_size, [y1, x1, y2, x2])
        gt_labels = (batch_size, padded_gt_boxes_size)
        gt_box_indices = (batch_size, total_pos_bboxes)

    outputs:
        roi_bbox_deltas = (batch_size, total_pos_bboxes + total_neg_bboxes, total_labels * [delta_y, delta_x, delta_h, delta_w])
        roi_bbox_labels = (batch_size, total_pos_bboxes + total_neg_bboxes, total_labels)
    """

    def __init__(self, hyper_params, **kwargs):
        super(RoIDelta, self).__init__(**kwargs)
        self.hyper_params = hyper_params

    def get_config(self):
        config = super(RoIDelta, self).get_config()
        config.update({"hyper_params": self.hyper_params})
        return config

    def call(self, inputs):
        roi_bboxes = inputs[0]
        gt_boxes = inputs[1]
        gt_labels = inputs[2]
        gt_box_indices = inputs[3]
        total_labels = self.hyper_params["total_labels"]
        total_pos_bboxes = self.hyper_params["total_pos_bboxes"]
        total_neg_bboxes = self.hyper_params["total_neg_bboxes"]
        total_bboxes = total_pos_bboxes + total_neg_bboxes
        batch_size = tf.shape(roi_bboxes)[0]
        #
        gt_boxes_map = helpers.get_gt_boxes_map(gt_boxes, gt_box_indices, batch_size, total_neg_bboxes)
        #
        pos_gt_labels_map = tf.gather(gt_labels, gt_box_indices, batch_dims=1)
        neg_gt_labels_map = tf.fill((batch_size, total_neg_bboxes), total_labels-1)
        gt_labels_map = tf.concat([pos_gt_labels_map, neg_gt_labels_map], axis=1)
        #
        roi_bbox_deltas = helpers.get_deltas_from_bboxes(roi_bboxes, gt_boxes_map)
        #
        flatted_batch_indices = helpers.get_tiled_indices(batch_size, total_bboxes)
        flatted_bbox_indices = tf.reshape(tf.tile(tf.range(total_bboxes), (batch_size, )), (-1, 1))
        flatted_gt_labels_indices = tf.reshape(gt_labels_map, (-1, 1))
        scatter_indices = helpers.get_scatter_indices_for_bboxes([flatted_batch_indices, flatted_bbox_indices, flatted_gt_labels_indices], batch_size, total_bboxes)
        roi_bbox_deltas = tf.scatter_nd(scatter_indices, roi_bbox_deltas, (batch_size, total_bboxes, total_labels, 4))
        roi_bbox_deltas = tf.reshape(roi_bbox_deltas, (batch_size, total_bboxes, total_labels * 4))
        roi_bbox_labels = tf.scatter_nd(scatter_indices, tf.ones((batch_size, total_bboxes), tf.int32), (batch_size, total_bboxes, total_labels))
        #
        return tf.stop_gradient(roi_bbox_deltas), tf.stop_gradient(roi_bbox_labels)

class RoIPooling(Layer):
    """Reducing all feature maps to same size.
    Firstly cropping bounding boxes from the feature maps and then resizing it to the pooling size.
    inputs:
        feature_map = (batch_size, img_output_height, img_output_width, channels)
        roi_bboxes = (batch_size, total_pos_bboxes + total_neg_bboxes, [y1, x1, y2, x2])

    outputs:
        final_pooling_feature_map = (batch_size, total_pos_bboxes + total_neg_bboxes, pooling_size[0], pooling_size[1], channels)
            pooling_size usually (7, 7)
    """

    def __init__(self, hyper_params, **kwargs):
        super(RoIPooling, self).__init__(**kwargs)
        self.hyper_params = hyper_params

    def get_config(self):
        config = super(RoIPooling, self).get_config()
        config.update({"hyper_params": self.hyper_params})
        return config

    def call(self, inputs):
        feature_map = inputs[0]
        roi_bboxes = inputs[1]
        total_pos_bboxes = self.hyper_params["total_pos_bboxes"]
        total_neg_bboxes = self.hyper_params["total_neg_bboxes"]
        pooling_size = self.hyper_params["pooling_size"]
        total_bboxes = total_pos_bboxes + total_neg_bboxes
        batch_size = tf.shape(roi_bboxes)[0]
        #
        row_size = batch_size * total_bboxes
        # We need to arange bbox indices for each batch
        flatted_batch_indices = helpers.get_tiled_indices(batch_size, total_bboxes)
        pooling_bbox_indices = tf.reshape(flatted_batch_indices, (-1, ))
        pooling_bboxes = tf.reshape(roi_bboxes, (row_size, 4))
        # Crop to bounding box size then resize to pooling size
        pooling_feature_map = tf.image.crop_and_resize(
            feature_map,
            pooling_bboxes,
            pooling_bbox_indices,
            pooling_size
        )
        final_pooling_feature_map = tf.reshape(pooling_feature_map, (batch_size, total_bboxes, pooling_feature_map.shape[1], pooling_feature_map.shape[2], pooling_feature_map.shape[3]))
        return final_pooling_feature_map

def get_valid_predictions(roi_bboxes, frcnn_delta_pred, frcnn_label_pred, total_labels):
    """Generating valid detections from faster rcnn predictions removing backgroud predictions.
    Batch size should be 1 for this method.
    inputs:
        roi_bboxes = (batch_size, total_pos_bboxes + total_neg_bboxes, [y1, x1, y2, x2])
        frcnn_delta_pred = (batch_size, total_pos_bboxes + total_neg_bboxes, total_labels * [delta_y, delta_x, delta_h, delta_w])
        frcnn_label_pred = (batch_size, total_pos_bboxes + total_neg_bboxes, total_labels)
        total_labels = number, 20 + 1 for VOC dataset +1 for background label

    outputs:
        valid_pred_bboxes = (batch_size, total_valid_bboxes, total_labels, [y1, x1, y2, x2])
        valid_labels = (batch_size, total_valid_bboxes, total_labels)
    """
    pred_labels_map = tf.argmax(frcnn_label_pred, 2, output_type=tf.int32)
    #
    valid_label_indices = tf.where(tf.not_equal(pred_labels_map, total_labels-1))
    total_valid_bboxes = tf.shape(valid_label_indices)[0]
    #
    valid_roi_bboxes = tf.gather_nd(roi_bboxes, valid_label_indices)
    valid_deltas = tf.gather_nd(frcnn_delta_pred, valid_label_indices)
    valid_deltas = tf.reshape(valid_deltas, (total_valid_bboxes, total_labels, 4))
    valid_labels = tf.gather_nd(frcnn_label_pred, valid_label_indices)
    #
    valid_labels_map = tf.gather_nd(pred_labels_map, valid_label_indices)
    #
    flatted_bbox_indices = tf.reshape(tf.range(total_valid_bboxes), (-1, 1))
    flatted_labels_indices = tf.reshape(valid_labels_map, (-1, 1))
    scatter_indices = tf.concat([flatted_bbox_indices, flatted_labels_indices], 1)
    scatter_indices = tf.reshape(scatter_indices, (total_valid_bboxes, 2))
    valid_roi_bboxes = tf.scatter_nd(scatter_indices, valid_roi_bboxes, (total_valid_bboxes, total_labels, 4))
    valid_pred_bboxes = helpers.get_bboxes_from_deltas(valid_roi_bboxes, valid_deltas)
    return tf.expand_dims(valid_pred_bboxes, 0), tf.expand_dims(valid_labels, 0)

def generator(dataset, hyper_params, input_processor):
    """Tensorflow data generator for fit method, yielding inputs and outputs.
    inputs:
        dataset = tf.data.Dataset, PaddedBatchDataset
        hyper_params = dictionary
        input_processor = function for preparing image for input. It's getting from backbone.

    outputs:
        yield inputs, outputs
    """
    while True:
        for image_data in dataset:
            _, gt_boxes, gt_labels = image_data
            input_img, bbox_deltas, bbox_labels, anchors = rpn.get_step_data(image_data, hyper_params, input_processor)
            yield (input_img, anchors, gt_boxes, gt_labels, bbox_deltas, bbox_labels), ()

def get_model(base_model, rpn_model, hyper_params, mode="training"):
    """Generating rpn model for given backbone base model and hyper params.
    inputs:
        base_model = tf.keras.model pretrained backbone, only VGG16 available for now
        rpn_model = tf.keras.model generated rpn model
        hyper_params = dictionary
        mode = "training" or "inference"

    outputs:
        frcnn_model = tf.keras.model
    """
    input_img = base_model.input
    rpn_reg_predictions, rpn_cls_predictions = rpn_model.output
    #
    input_anchors = Input(shape=(None, 4), name="input_anchors", dtype=tf.float32)
    input_gt_boxes = Input(shape=(None, 4), name="input_gt_boxes", dtype=tf.float32)
    #
    roi_bboxes, gt_box_indices = RoIBBox(hyper_params, name="roi_bboxes")(
                                        [rpn_reg_predictions, rpn_cls_predictions, input_anchors, input_gt_boxes])
    #
    roi_pooled = RoIPooling(hyper_params, name="roi_pooling")([base_model.output, roi_bboxes])
    #
    output = TimeDistributed(Flatten(), name="frcnn_flatten")(roi_pooled)
    output = TimeDistributed(Dense(4096, activation="relu"), name="frcnn_fc1")(output)
    output = TimeDistributed(BatchNormalization(), name="frcnn_batch_norm1")(output)
    output = TimeDistributed(Dropout(0.2), name="frcnn_dropout1")(output)
    output = TimeDistributed(Dense(2048, activation="relu"), name="frcnn_fc2")(output)
    output = TimeDistributed(BatchNormalization(), name="frcnn_batch_norm2")(output)
    output = TimeDistributed(Dropout(0.2), name="frcnn_dropout2")(output)
    frcnn_cls_predictions = TimeDistributed(Dense(hyper_params["total_labels"], activation="softmax"), name="frcnn_cls")(output)
    frcnn_reg_predictions = TimeDistributed(Dense(hyper_params["total_labels"] * 4, activation="linear"), name="frcnn_reg")(output)
    #
    if mode == "training":
        rpn_cls_actuals = Input(shape=(None, None, hyper_params["anchor_count"]), name="input_rpn_cls_actuals", dtype=tf.int32)
        rpn_reg_actuals = Input(shape=(None, None, hyper_params["anchor_count"] * 4), name="input_rpn_reg_actuals", dtype=tf.float32)
        input_gt_labels = Input(shape=(None, ), name="input_gt_labels", dtype=tf.int32)
        frcnn_reg_actuals, frcnn_cls_actuals = RoIDelta(hyper_params, name="roi_deltas")(
                                                        [roi_bboxes, input_gt_boxes, input_gt_labels, gt_box_indices])
        #
        loss_names = ["rpn_reg_loss", "rpn_cls_loss", "frcnn_reg_loss", "frcnn_cls_loss"]
        rpn_reg_loss_layer = Lambda(helpers.reg_loss, name=loss_names[0])([rpn_reg_actuals, rpn_reg_predictions])
        rpn_cls_loss_layer = Lambda(helpers.rpn_cls_loss, name=loss_names[1])([rpn_cls_actuals, rpn_cls_predictions])
        frcnn_reg_loss_layer = Lambda(helpers.reg_loss, name=loss_names[2])([frcnn_reg_actuals, frcnn_reg_predictions])
        frcnn_cls_loss_layer = Lambda(helpers.frcnn_cls_loss, name=loss_names[3])([frcnn_cls_actuals, frcnn_cls_predictions])
        #
        frcnn_model = Model(inputs=[input_img, input_anchors, input_gt_boxes, input_gt_labels,
                              rpn_reg_actuals, rpn_cls_actuals],
                      outputs=[roi_bboxes, rpn_reg_predictions, rpn_cls_predictions,
                               frcnn_reg_predictions, frcnn_cls_predictions,
                               rpn_reg_loss_layer, rpn_cls_loss_layer,
                               frcnn_reg_loss_layer, frcnn_cls_loss_layer])
        #
        for layer_name in loss_names:
            layer = frcnn_model.get_layer(layer_name)
            frcnn_model.add_loss(layer.output)
            frcnn_model.add_metric(layer.output, name=layer_name, aggregation="mean")
        #
    else:
        frcnn_model = Model(inputs=[input_img, input_anchors, input_gt_boxes],
                      outputs=[roi_bboxes, rpn_reg_predictions, rpn_cls_predictions,
                               frcnn_reg_predictions, frcnn_cls_predictions])
        #
    dummy_initializer = get_dummy_initializer(hyper_params, mode)
    frcnn_model(dummy_initializer)
    return frcnn_model

def get_dummy_initializer(hyper_params, mode="training"):
    """Generating dummy data for initialize model.
    In this way, the training process can continue from where it left off.
    inputs:
        hyper_params = dictionary
        mode = "training" or "inference"

    outputs:
        dummy data for model initialization
    """
    final_height, final_width = helpers.VOC["max_height"], helpers.VOC["max_width"]
    img = tf.random.uniform((1, final_height, final_width, 3))
    output_height, output_width = final_height // hyper_params["stride"], final_width // hyper_params["stride"]
    total_anchors = output_height * output_width * hyper_params["anchor_count"]
    anchors = tf.random.uniform((1, total_anchors, 4))
    gt_boxes = tf.random.uniform((1, 1, 4))
    if mode != "training":
        return img, anchors, gt_boxes
    gt_labels = tf.random.uniform((1, 1), maxval=hyper_params["total_labels"], dtype=tf.int32)
    bbox_deltas = tf.random.uniform((1, output_height, output_width, hyper_params["anchor_count"] * 4))
    bbox_labels = tf.random.uniform((1, output_height, output_width, hyper_params["anchor_count"]), maxval=hyper_params["total_labels"], dtype=tf.int32)
    return img, anchors, gt_boxes, gt_labels, bbox_deltas, bbox_labels
