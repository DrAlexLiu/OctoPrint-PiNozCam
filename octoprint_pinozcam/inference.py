import numpy as np
import onnxruntime
import time
import os

def _generate_anchors(stride, ratio_vals, scales_vals, angles_vals=None):
    """Generate anchor coordinates based on scales and ratios using Numpy.

    Args:
        stride (int): The stride of the feature map.
        ratio_vals (list of float): List of aspect ratios for the anchors.
        scales_vals (list of float): List of scales for the anchors.
        angles_vals (list of float, optional): List of angles for rotated anchors. Defaults to None.

    Returns:
        np.ndarray: An array containing the coordinates of the generated anchors.
    """
    scales = np.tile(scales_vals, (len(ratio_vals), 1))
    scales = np.transpose(scales).reshape(-1, 1)
    ratios = np.tile(ratio_vals, len(scales_vals))

    wh = np.tile(stride, (len(ratios), 2))
    ws = np.sqrt(wh[:, 0] * wh[:, 1] / ratios)

    dwh = np.stack([ws, ws * ratios], axis=1)
    
    xy1 = 0.5 * (wh - dwh * scales)
    xy2 = 0.5 * (wh + dwh * scales)
    
    return np.concatenate([xy1, xy2], axis=1)

def _nms(all_scores, all_boxes, all_classes, nms=0.5, ndetections=100):
    """
    Apply Non-Maximum Suppression (NMS) to prediction boxes to eliminate redundant overlapping boxes.

    Non-Maximum Suppression is a key post-processing step in object detection algorithms to select the most
    probable bounding box for an object when multiple boxes predict its presence.

    Parameters:
    - all_scores (np.ndarray): A numpy array of shape (batch_size, num_predictions) containing the scores of each prediction.
    - all_boxes (np.ndarray): A numpy array of shape (batch_size, num_predictions, 4) containing the coordinates of each prediction box.
    - all_classes (np.ndarray): A numpy array of shape (batch_size, num_predictions) containing the class IDs of each prediction.
    - nms (float): The Non-Maximum Suppression threshold for overlap. Predictions with IoU (Intersection over Union) over this threshold will be suppressed.
    - ndetections (int): The maximum number of detections to return after NMS.

    Returns:
    - out_scores, out_boxes, out_classes (tuple of np.ndarray): The scores, boxes, and classes after applying NMS,
      each of shape (batch_size, ndetections) or (batch_size, ndetections, 4) for boxes.
    """

    batch_size = all_scores.shape[0]
    out_scores = np.zeros((batch_size, ndetections))
    out_boxes = np.zeros((batch_size, ndetections, 4))
    out_classes = np.zeros((batch_size, ndetections))

    for batch in range(batch_size):
        # Filter out scores that are zero or below
        keep = all_scores[batch, :] > 0
        scores = all_scores[batch, keep]
        boxes = all_boxes[batch, keep, :]
        classes = all_classes[batch, keep]

        if scores.size == 0:
            continue

        # Sort the scores in descending order and sort boxes and classes accordingly
        indices = np.argsort(-scores)
        scores = scores[indices]
        boxes = boxes[indices, :]
        classes = classes[indices]

        # Round the class scores to the specified number of decimal places
        classes = np.round(classes[indices]).astype(int)
        # Calculate the area of each box for IoU computation
        areas = (boxes[:, 2] - boxes[:, 0] + 1) * (boxes[:, 3] - boxes[:, 1] + 1)
        keep = np.ones(scores.shape[0], dtype=bool)

        for i in range(ndetections):
            if not np.any(keep) or i >= scores.size:
                break

            # Compute intersection areas
            xy1 = np.maximum(boxes[:, :2], boxes[i, :2])
            xy2 = np.minimum(boxes[:, 2:], boxes[i, 2:])
            inter = np.prod(np.maximum(0, xy2 - xy1 + 1), axis=1)

            criterion = ((scores > scores[i]) |
                         (inter / (areas + areas[i] - inter) <= nms) |
                         (classes != classes[i]))

            criterion[i] = True
            keep &= criterion

        # Applying the keep mask to scores, boxes, and classes
        keep_indices = np.where(keep)[0]

        final_count = min(ndetections, keep_indices.size)
        out_scores[batch, :final_count] = scores[keep_indices][:final_count]
        out_boxes[batch, :final_count, :] = boxes[keep_indices, :][:final_count, :]
        out_classes[batch, :final_count] = classes[keep_indices][:final_count]
    
    return out_scores, out_boxes, out_classes

def _delta2box(deltas, anchors, size, stride):
    """
    Convert deltas from anchors to boxes using Numpy.

    Args:
        deltas (np.ndarray): The deltas between the anchors and the ground truth boxes.
        anchors (np.ndarray): The anchor boxes.
        size (list): The size of the feature map.
        stride (int): The stride of the feature map.

    Returns:
        np.ndarray: The converted bounding boxes.
    """
    # Calculate the width and height of the anchors
    anchors_wh = anchors[:, 2:] - anchors[:, :2] + 1
    # Calculate the center of the anchors
    ctr = anchors[:, :2] + 0.5 * anchors_wh
    # Predict the center of the bounding boxes
    pred_ctr = deltas[:, :2] * anchors_wh + ctr
    # Predict the width and height of the bounding boxes
    pred_wh = np.exp(deltas[:, 2:]) * anchors_wh

    # Define the minimum and maximum values for clamping
    m = np.zeros([2], dtype=deltas.dtype)
    M = np.array([size], dtype=deltas.dtype) * stride - 1
    # Clamping function to ensure the bounding boxes are within the image boundaries
    def clamp(t): return np.maximum(m, np.minimum(t, M))
    
    # Return the converted bounding boxes
    return np.concatenate([
        clamp(pred_ctr - 0.5 * pred_wh),
        clamp(pred_ctr + 0.5 * pred_wh - 1)
    ], axis=1)

def _decode(all_cls_head, all_box_head, stride=1, threshold=0.05, top_n=1000, anchors=None, rotated=False):
    """
    Decode bounding boxes and filter based on score threshold.
    """
    if rotated:
        anchors = anchors[0]
    num_boxes = 4 if not rotated else 6

    batch_size, _, height, width = all_cls_head.shape
    num_anchors = anchors.shape[0] if anchors is not None else 1
    num_classes = all_cls_head.shape[1] // num_anchors

    out_scores = []
    out_boxes = []
    out_classes = []

    for batch in range(batch_size):
        cls_head = all_cls_head[batch].reshape(-1)
        box_head = all_box_head[batch].reshape(-1, num_boxes)

        keep = np.where(cls_head >= threshold)[0]
        if keep.size == 0:
            continue

        scores = cls_head[keep]
        indices = scores.argsort()[::-1][:top_n]
        scores = scores[indices]
        classes = (keep[indices] / width / height) % num_classes

        x = (keep[indices] % width).astype(np.int64)
        y = ((keep[indices] / width) % height).astype(np.int64)
        a = (keep[indices] / num_classes / height / width).astype(np.int64)

        boxes = box_head[keep[indices]]

        if anchors is not None:
            grid = np.stack([x, y, x, y], axis=1) * stride + anchors[a]
            boxes = _delta2box(boxes, grid, [width, height], stride)

        out_scores.append(scores)
        out_boxes.append(boxes)
        out_classes.append(classes)

    return np.array(out_scores), np.array(out_boxes), np.array(out_classes)

def _detection_postprocess(image, cls_heads, box_heads):
    """
    Post-process detection outputs using Numpy for decoding and processing

    This function decodes class and box predictions into human-readable formats,
    concatenates results from all heads, and prepares them for further processing
    or analysis.

    Args:
        image (np.ndarray): The input image tensor with shape (B, C, H, W).
        cls_heads (list of np.ndarray): List of class head tensors, each with shape (B, A, H', W').
        box_heads (list of np.ndarray): List of box head tensors, each with shape (B, A*4, H', W').

    Returns:
        tuple: Tuple containing:
            - scores (np.ndarray): Scores of the detected boxes with shape (B, N).
            - boxes (np.ndarray): Detected bounding boxes with shape (B, N, 4).
            - labels (np.ndarray): Class labels for the detected boxes with shape (B, N).
    """

    # Dictionary to store anchors based on stride
    anchors = {}
    decoded = []

    for cls_head, box_head in zip(cls_heads, box_heads):
        # Calculate the stride based on the image and class head dimensions
        stride = image.shape[-1] // cls_head.shape[-1]

        # Generate anchors if they haven't been generated for this stride
        if stride not in anchors:
            anchors[stride] = _generate_anchors(stride, ratio_vals=[1.0, 2.0, 0.5], scales_vals=[4 * 2 ** (i / 3) for i in range(3)])

        # Decode the class and box heads into human-readable format using Numpy
        scores, boxes, classes = _decode(cls_head, box_head, stride, 0.05, 1000, anchors[stride])
        if scores.size > 0:  # Only add non-empty detections
            decoded.append((scores, boxes, classes))
    
    # Handle cases where no detections meet the threshold
    if not decoded:
        return np.zeros((1, 6)), np.zeros((1, 6, 4)), np.zeros((1, 6))

    # Process decoded results
    scores_list, boxes_list, classes_list = [], [], []
    batch_size = image.shape[0]

    for scores, boxes, classes in decoded:
        if scores.size == 0:
            continue
        scores_list.append(scores)
        boxes_list.append(boxes)
        classes_list.append(classes)

    # Concatenation while preserving the batch dimension
    all_scores = np.concatenate([arr for arr in scores_list], axis=1)
    all_boxes = np.concatenate([arr for arr in boxes_list], axis=1)
    all_classes = np.concatenate([arr for arr in classes_list], axis=1)

    # Apply non-maximum suppression to remove overlapping boxes, using the original _nms function or a modified version for Numpy
    scores, boxes, labels = _nms(all_scores, all_boxes, all_classes, nms=0.5, ndetections=6)

    return scores, boxes, labels

def preprocess_image(image):
    """
    Preprocesses the input image for inference.

    Args:
        image (PIL.Image.Image): The input image to preprocess.

    Returns:
        np.ndarray: The preprocessed image as a Numpy array.
    """
    
    img_arr = np.array(image).astype(np.float32) / 255.0
    
    img_arr = np.transpose(img_arr, (2, 0, 1))
    
    mean = np.array([0.485, 0.456, 0.406]).reshape((3, 1, 1))
    std = np.array([0.229, 0.224, 0.225]).reshape((3, 1, 1))
    img_arr = (img_arr - mean) / std
    return img_arr

def image_inference(input_image, scores_threshold, img_sensitivity,num_threads=2):
    """
    Performs inference on the given image using a pre-trained ONNX model.

    Inputs:
    - input_image (PIL.Image.Image): The input image on which inference is to be performed.
    - scores_threshold (float): Threshold for filtering boxes based on scores.
    - img_sensitivity (float): Sensitivity value used for calculating severity.   

    Outputs:
    - scores (numpy.ndarray): Confidence scores for each detected box.
    - scaled_boxes (numpy.ndarray): Detected bounding boxes scaled to the original image dimensions.
    - labels (numpy.ndarray): Class labels for each detected box.
    - severity (numpy.ndarray): Calculated severity value based on the percentage of area covered by boxes.
    - elapsed_time (float): Time taken for the inference in seconds.

    """

    # model internal parameters
    _proc_img_width =640
    _proc_img_height=384
    
    bin_file="nozcam.bin"
    
    bin_file_path = os.path.join(os.path.dirname(__file__),'static', bin_file)


    # Get the width and height of the image
    img_width, img_height = input_image.size

    # Resize the image
    input_image = input_image.resize((_proc_img_width, _proc_img_height))
    
    input_array = preprocess_image(input_image)
    input_array = input_array.astype(np.float32)
    # create the batch
    input_batch = np.expand_dims(input_array, axis=0)

    # Start the timer
    start_time = time.time()
    # Run the ONNX model inference
    sess_opt = onnxruntime.SessionOptions()
    sess_opt.intra_op_num_threads = num_threads
    ort_session = onnxruntime.InferenceSession(bin_file_path, sess_opt, providers=['CPUExecutionProvider'])

    ort_inputs = {ort_session.get_inputs()[0].name: input_batch}
    ort_outs = ort_session.run(None, ort_inputs)

    # Stop the timer
    end_time = time.time()
    # Calculate the elapsed time
    elapsed_time = end_time - start_time
    

    # Split the output into classification and box regression heads
    cls_heads = ort_outs[:5]
    box_heads = ort_outs[5:10]

    scores, boxes, labels = _detection_postprocess(input_array, cls_heads, box_heads)

    # Create a 2D array (bitmap) representing the image
    bitmap = [[False for _ in range(_proc_img_width)] for _ in range(_proc_img_height)]
    
    # Filter boxes based on scores_threshold and label constraints
    filtered_boxes = [box for score, box in zip(scores[0], boxes[0]) 
                    if score > scores_threshold]
    

    for box in filtered_boxes:
        x1, y1, x2, y2 = map(int, box)
        # Clip the coordinates to stay within the image boundaries
        x1 = max(0, min(x1, _proc_img_width - 1))
        y1 = max(0, min(y1, _proc_img_height - 1))
        x2 = max(0, min(x2, _proc_img_width - 1))
        y2 = max(0, min(y2, _proc_img_height - 1))

        for i in range(y1, y2):
            for j in range(x1, x2):
                bitmap[i][j] = True

    # Count the number of ones in the bitmap
    total_area = sum(row.count(True) for row in bitmap)

    # Calculate the percentage of the total area covered by the boxes
    percentage_area = total_area / (_proc_img_width * _proc_img_height)
    
    # Divide by img_sensitivity
    severity = max(0, min(percentage_area / img_sensitivity, 1.0))
    #severity = np.array([severity])

    # Calculate scaling factors
    height_scale = img_height / _proc_img_height
    width_scale = img_width / _proc_img_width

    # Scale the boxes to the original size of picture
    scaled_boxes = [[[x1 * width_scale, y1 * height_scale, x2 * width_scale, y2 * height_scale] for x1, y1, x2, y2 in box] for box in boxes]

    return scores.tolist(), scaled_boxes, labels.tolist(), severity, percentage_area, elapsed_time

