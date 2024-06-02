import numpy as np
import time

def _generate_anchors(stride, ratio_vals, scales_vals):
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
    - all_scores (np.ndarray): A numpy array of shape (num_predictions,) containing the scores of each prediction.
    - all_boxes (np.ndarray): A numpy array of shape (num_predictions, 4) containing the coordinates of each prediction box.
    - all_classes (np.ndarray): A numpy array of shape (num_predictions,) containing the class IDs of each prediction.
    - nms (float): The Non-Maximum Suppression threshold for overlap. Predictions with IoU (Intersection over Union) over this threshold will be suppressed.
    - ndetections (int): The maximum number of detections to return after NMS.

    Returns:
    - out_scores, out_boxes, out_classes (np.ndarray): The scores, boxes, and classes after applying NMS,
      each of shape (num_actual_detections,) or (num_actual_detections, 4) for boxes, where num_actual_detections <= ndetections.
    """

    out_scores = np.zeros((ndetections,))
    out_boxes = np.zeros((ndetections, 4))
    out_classes = np.zeros((ndetections,))

    # Discard null scores
    keep = (all_scores > 0)
    scores = all_scores[keep]
    boxes = all_boxes[keep]
    classes = all_classes[keep]

    if scores.size == 0:
        return out_scores, out_boxes, out_classes

    # Sort boxes
    indices = np.argsort(-scores)
    scores = scores[indices]
    boxes = boxes[indices]
    classes = classes[indices]

    areas = (boxes[:, 2] - boxes[:, 0] + 1) * (boxes[:, 3] - boxes[:, 1] + 1)
    keep = np.ones(scores.size, dtype=bool)
    for i in range(ndetections):
        if i >= keep.sum() or i >= scores.size:
            i -= 1
            break

        # Find overlapping boxes with lower score
        xy1 = np.maximum(boxes[:, :2], boxes[i, :2])
        xy2 = np.minimum(boxes[:, 2:], boxes[i, 2:])
        inter = np.prod(np.maximum(0, xy2 - xy1 + 1), axis=1)

        criterion = ((scores > scores[i]) |
                     (inter / (areas + areas[i] - inter) <= nms) |
                     (classes != classes[i]))
        criterion[i] = True

        # Only keep relevant boxes
        scores = scores[criterion]
        boxes = boxes[criterion]
        classes = classes[criterion]
        areas = areas[criterion]
        keep = keep[criterion]

    out_scores[:i + 1] = scores[:i + 1]
    out_boxes[:i + 1] = boxes[:i + 1]
    out_classes[:i + 1] = classes[:i + 1]

    return out_scores[:i + 1], out_boxes[:i + 1], out_classes[:i + 1]

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

def _decode(all_cls_head, all_box_head, stride=1, threshold=0.05, top_n=1000, anchors=None):
    """
    Decode the output of the object detection model and filter the results based on a score threshold.

    Args:
        all_cls_head (numpy.ndarray): The class prediction output of the model with shape (num_anchors, height, width).
        all_box_head (numpy.ndarray): The bounding box prediction output of the model with shape (num_anchors * 4, height, width).
        stride (int): The stride of the model's output feature map. Default is 1.
        threshold (float): The score threshold for filtering the results. Default is 0.05.
        top_n (int): The maximum number of top-scoring predictions to keep. Default is 1000.
        anchors (numpy.ndarray, optional): The anchor boxes used by the model. Default is None.

    Returns:
        tuple: A tuple containing three elements:
            - scores (numpy.ndarray): The scores of the filtered predictions.
            - boxes (numpy.ndarray): The bounding boxes of the filtered predictions.
            - classes (numpy.ndarray): The class labels of the filtered predictions.
    """
    num_boxes = 4

    _, height, width = all_cls_head.shape
    num_anchors = anchors.shape[0] if anchors is not None else 1
    num_classes = all_cls_head.shape[0] // num_anchors

    cls_head = all_cls_head.reshape(-1)
    box_head = all_box_head.reshape(-1, num_boxes)

    keep = np.where(cls_head >= threshold)[0]
    if keep.size == 0:
        return np.array([]), np.array([]), np.array([])

    scores = cls_head[keep]
    indices = scores.argsort()[::-1][:top_n]
    scores = scores[indices]
    classes = keep[indices] // (width * height * num_anchors) + 1

    x = (keep[indices] % width).astype(np.int64)
    y = ((keep[indices] / width) % height).astype(np.int64)
    a = (keep[indices] / num_classes / height / width).astype(np.int64)

    boxes = box_head[keep[indices]]

    grid = np.stack([x, y, x, y], axis=1) * stride + anchors[a]
    boxes = _delta2box(boxes, grid, [width, height], stride)
    
    return scores, boxes, classes

def _detection_postprocess(_proc_img_width, cls_heads, box_heads):
    """
    Post-process detection outputs using Numpy for decoding and processing

    This function decodes class and box predictions into human-readable formats,
    concatenates results from all heads, and prepares them for further processing
    or analysis.

    Args:
        _proc_img_width (int): The width of the processed image.
        cls_heads (list of np.ndarray): List of class head tensors, each with shape (A, H', W').
        box_heads (list of np.ndarray): List of box head tensors, each with shape (A*4, H', W').

    Returns:
        - scores (np.ndarray): Scores of the detected boxes with shape (N,).
        - boxes (np.ndarray): Detected bounding boxes with shape (N, 4).
        - labels (np.ndarray): Class labels for the detected boxes with shape (N,).
    """

    # Dictionary to store anchors based on stride
    anchors = {}
    decoded = []

    for cls_head, box_head in zip(cls_heads, box_heads):
        # Calculate the stride based on the image and class head dimensions
        stride = _proc_img_width // cls_head.shape[-1]

        # Generate anchors if they haven't been generated for this stride
        if stride not in anchors:
            anchors[stride] = _generate_anchors(stride, ratio_vals=[1.0, 2.0, 0.5], scales_vals=[4 * 2 ** (i / 3) for i in range(3)])

        # Decode the class and box heads into human-readable format using Numpy
        scores, boxes, classes = _decode(cls_head, box_head, stride, 0.05, 1000, anchors[stride])
        if scores.size > 0:  # Only add non-empty detections
            decoded.append((scores, boxes, classes))
    
    # Handle cases where no detections meet the threshold
    if not decoded:
        return np.array([]), np.array([]), np.array([])

    # Concatenate the decoded results
    all_scores, all_boxes, all_classes = zip(*decoded)
    all_scores = np.concatenate(all_scores, axis=0)
    all_boxes = np.concatenate(all_boxes, axis=0)
    all_classes = np.concatenate(all_classes, axis=0)

    # Apply non-maximum suppression to remove overlapping boxes, using the original _nms function or a modified version for Numpy
    scores, boxes, labels = _nms(all_scores, all_boxes, all_classes, nms=0.5, ndetections=6)

    return scores, boxes, labels

def _preprocess_image(image):
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

def image_inference(input_image, scores_threshold, img_sensitivity, 
                    ort_session,
                    _proc_img_width=640, _proc_img_height=384):
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
    #_proc_img_width = 640
    #_proc_img_height = 384

    # Get the width and height of the image
    img_width, img_height = input_image.size

    # Resize the image
    input_image = input_image.resize((_proc_img_width, _proc_img_height))
    
    input_array = _preprocess_image(input_image)
    input_array = input_array.astype(np.float32)
    # create the batch
    input_batch = np.expand_dims(input_array, axis=0)

    # Start the timer
    start_time = time.time()

    # Run the ONNX model inference
    ort_inputs = {ort_session.get_inputs()[0].name: input_batch}
    ort_outs = ort_session.run(None, ort_inputs)

    # Stop the timer
    end_time = time.time()
    # Calculate the elapsed time
    elapsed_time = end_time - start_time
    
    ort_outs = [out[0] for out in ort_outs]

    # Split the output into classification and box regression heads
    cls_heads = ort_outs[:5]
    box_heads = ort_outs[5:]

    scores, boxes, labels = _detection_postprocess(_proc_img_width, cls_heads, box_heads)

    # Create a 2D array (bitmap) representing the image
    bitmap = [[False for _ in range(_proc_img_width)] for _ in range(_proc_img_height)]
    
    # Filter boxes based on scores_threshold
    filtered_boxes = [box for score, box in zip(scores, boxes) if score > scores_threshold]
    
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

    # Calculate scaling factors
    height_scale = img_height / _proc_img_height
    width_scale = img_width / _proc_img_width

    # Scale the boxes to the original size of picture
    scaled_boxes = [[x1 * width_scale, y1 * height_scale, x2 * width_scale, y2 * height_scale] for x1, y1, x2, y2 in boxes]

    return scores, scaled_boxes, labels, severity, percentage_area, elapsed_time

