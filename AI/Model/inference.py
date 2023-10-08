import numpy as np
import onnxruntime
from torchvision import transforms
import torch
import time
import multiprocessing
import math

def _generate_anchors(stride, ratio_vals, scales_vals, angles_vals=None):
    """Generate anchor coordinates based on scales and ratios.

    This function computes anchor boxes for a given stride, scale, and ratio.
    The anchors are used in object detection algorithms to predict bounding boxes.

    Args:
        stride (int): The stride of the feature map.
        ratio_vals (list of float): List of aspect ratios for the anchors.
        scales_vals (list of float): List of scales for the anchors.
        angles_vals (list of float, optional): List of angles for rotated anchors. Defaults to None.

    Returns:
        torch.Tensor: A tensor containing the coordinates of the generated anchors.

    """
    
    # Convert scales and ratios to tensors
    scales = torch.FloatTensor(scales_vals).repeat(len(ratio_vals), 1)
    scales = scales.transpose(0, 1).contiguous().view(-1, 1)
    ratios = torch.FloatTensor(ratio_vals * len(scales_vals))

    # Compute width and height for the anchors based on stride
    wh = torch.FloatTensor([stride]).repeat(len(ratios), 2)
    ws = torch.sqrt(wh[:, 0] * wh[:, 1] / ratios)
    
    # Compute the dimensions of the anchor boxes
    dwh = torch.stack([ws, ws * ratios], dim=1)
    
    # Compute the top-left and bottom-right coordinates of the anchor boxes
    xy1 = 0.5 * (wh - dwh * scales)
    xy2 = 0.5 * (wh + dwh * scales)
    
    return torch.cat([xy1, xy2], dim=1)


def _nms_bak(all_scores, all_boxes, all_classes, nms=0.5, ndetections=100):
    """Apply Non-Maximum Suppression (NMS) on prediction boxes.

    This function suppresses boxes that have a high overlap with other boxes 
    that have higher scores.

    Args:
        all_scores (torch.Tensor): Tensor of shape [batch_size, num_boxes] containing scores.
        all_boxes (torch.Tensor): Tensor of shape [batch_size, num_boxes, 4] containing bounding box coordinates.
        all_classes (torch.Tensor): Tensor of shape [batch_size, num_boxes] containing class labels.
        nms (float, optional): Overlap threshold for NMS. Defaults to 0.5.
        ndetections (int, optional): Maximum number of detections to keep. Defaults to 100.

    Returns:
        tuple: Three tensors containing the scores, boxes, and classes after applying NMS.

    """
    
    device = all_scores.device
    batch_size = all_scores.size()[0]
    out_scores = torch.zeros((batch_size, ndetections), device=device)
    out_boxes = torch.zeros((batch_size, ndetections, 4), device=device)
    out_classes = torch.zeros((batch_size, ndetections), device=device)

    # Process each item in the batch
    for batch in range(batch_size):
        # Filter out boxes with null scores
        keep = (all_scores[batch, :].view(-1) > 0).nonzero()
        scores = all_scores[batch, keep].view(-1)
        boxes = all_boxes[batch, keep, :].view(-1, 4)
        classes = all_classes[batch, keep].view(-1)

        if scores.nelement() == 0:
            continue

        # Sort boxes based on scores in descending order
        scores, indices = torch.sort(scores, descending=True)
        boxes, classes = boxes[indices], classes[indices]
        
        # Compute areas of the boxes
        areas = (boxes[:, 2] - boxes[:, 0] + 1) * (boxes[:, 3] - boxes[:, 1] + 1).view(-1)
        keep = torch.ones(scores.nelement(), device=device, dtype=torch.uint8).view(-1)

        for i in range(ndetections):
            if i >= keep.nonzero().nelement() or i >= scores.nelement():
                i -= 1
                break

            # Identify boxes that overlap too much with the current box
            xy1 = torch.max(boxes[:, :2], boxes[i, :2])
            xy2 = torch.min(boxes[:, 2:], boxes[i, 2:])
            inter = torch.prod((xy2 - xy1 + 1).clamp(0), 1)
            criterion = ((scores > scores[i]) |
                         (inter / (areas + areas[i] - inter) <= nms) |
                         (classes != classes[i]))
            criterion[i] = 1

            # Keep boxes based on the criterion
            scores = scores[criterion.nonzero()].view(-1)
            boxes = boxes[criterion.nonzero(), :].view(-1, 4)
            classes = classes[criterion.nonzero()].view(-1)
            areas = areas[criterion.nonzero()].view(-1)
            keep[(~criterion).nonzero()] = 0

        # Store the post-NMS scores, boxes, and classes
        out_scores[batch, :i + 1] = scores[:i + 1]
        out_boxes[batch, :i + 1, :] = boxes[:i + 1, :]
        out_classes[batch, :i + 1] = classes[:i + 1]

    return out_scores, out_boxes, out_classes

def _nms(all_scores, all_boxes, all_classes, nms=0.5, ndetections=100, rounding_decimal=1):
    device = all_scores.device
    batch_size = all_scores.size()[0]
    out_scores = torch.zeros((batch_size, ndetections), device=device)
    out_boxes = torch.zeros((batch_size, ndetections, 4), device=device)
    out_classes = torch.zeros((batch_size, ndetections), device=device)

    for batch in range(batch_size):
        keep = all_scores[batch, :].nonzero(as_tuple=True)[0]
        scores, boxes, classes = all_scores[batch, keep], all_boxes[batch, keep], all_classes[batch, keep]

        if scores.nelement() == 0:
            continue

        # Round the class scores to the specified number of decimal places
        classes_rounded = torch.round(classes * 10**rounding_decimal) / 10**rounding_decimal

        scores, indices = torch.sort(scores, descending=True, dim=0)
        boxes, classes = boxes[indices], classes[indices]

        areas = (boxes[:, 2] - boxes[:, 0] + 1) * (boxes[:, 3] - boxes[:, 1] + 1)
        keep = torch.ones(scores.nelement(), device=device, dtype=torch.uint8)

        for i in range(ndetections):
            if i >= keep.nonzero().nelement() or i >= scores.nelement():
                i -= 1
                break

            xy1 = torch.max(boxes[:, :2], boxes[i, :2])
            xy2 = torch.min(boxes[:, 2:], boxes[i, 2:])
            inter = torch.prod((xy2 - xy1 + 1).clamp(0), 1)
            criterion = ((scores > scores[i]) |
                         (inter / (areas + areas[i] - inter) <= nms) |
                         (classes_rounded != classes_rounded[i]))
            criterion[i] = 1

            keep &= criterion

        keep_indices = keep.nonzero(as_tuple=True)[0]
        scores, boxes, classes = scores[keep_indices], boxes[keep_indices], classes[keep_indices]

        num_valid = len(scores)
        # Ensure that the slices match in size by selecting the first `ndetections` or `num_valid`, whichever is smaller.
        final_count = min(ndetections, num_valid)
        out_scores[batch, :final_count] = scores[:final_count]
        out_boxes[batch, :final_count, :] = boxes[:final_count, :]
        out_classes[batch, :final_count] = classes[:final_count]

    return out_scores, out_boxes, out_classes

def _delta2box(deltas, anchors, size, stride):
    """Convert deltas from anchors to boxes.

    This function converts the deltas (differences) from the anchors to 
    bounding boxes in the format (x1, y1, x2, y2).

    Args:
        deltas (torch.Tensor): The deltas between the anchors and the ground truth boxes.
        anchors (torch.Tensor): The anchor boxes.
        size (int): The size of the feature map.
        stride (int): The stride of the feature map.

    Returns:
        torch.Tensor: The converted bounding boxes.

    """
    
    # Calculate the width and height of the anchors
    anchors_wh = anchors[:, 2:] - anchors[:, :2] + 1
    # Calculate the center of the anchors
    ctr = anchors[:, :2] + 0.5 * anchors_wh
    # Predict the center of the bounding boxes
    pred_ctr = deltas[:, :2] * anchors_wh + ctr
    # Convert deltas to float32 for the exponential operation
    deltas_float32 = deltas.to(torch.float32)
    # Predict the width and height of the bounding boxes
    pred_wh = torch.exp(deltas_float32[:, 2:]) * anchors_wh

    # Define the minimum and maximum values for clamping
    m = torch.zeros([2], device=deltas.device, dtype=deltas.dtype)
    M = (torch.tensor([size], device=deltas.device, dtype=deltas.dtype) * stride - 1)
    # Define a clamping function to ensure the bounding boxes are within the image boundaries
    clamp = lambda t: torch.max(m, torch.min(t, M))
    
    # Return the converted bounding boxes
    return torch.cat([
        clamp(pred_ctr - 0.5 * pred_wh),
        clamp(pred_ctr + 0.5 * pred_wh - 1)
    ], 1)


def _decode(all_cls_head, all_box_head, stride=1, threshold=0.05, top_n=1000, anchors=None, rotated=False):
    """Decode bounding boxes and filter based on score threshold.

    This function decodes the bounding boxes from the predicted class and box 
    heads, and filters out boxes with scores below a given threshold.

    Args:
        all_cls_head (torch.Tensor): Predicted class heads.
        all_box_head (torch.Tensor): Predicted box heads.
        stride (int, optional): Stride of the feature map. Defaults to 1.
        threshold (float, optional): Score threshold for filtering boxes. Defaults to 0.05.
        top_n (int, optional): Maximum number of boxes to keep. Defaults to 1000.
        anchors (torch.Tensor, optional): Anchor boxes. Defaults to None.
        rotated (bool, optional): Whether the boxes are rotated. Defaults to False.

    Returns:
        tuple: Tuple containing:
            - out_scores (torch.Tensor): Scores of the decoded boxes.
            - out_boxes (torch.Tensor): Decoded bounding boxes.
            - out_classes (torch.Tensor): Class labels for the decoded boxes.

    """
    
    # Check if rotated and adjust anchor boxes accordingly
    if rotated:
        anchors = anchors[0]
    num_boxes = 4 if not rotated else 6

    # Set device, anchor type, and get dimensions
    device = all_cls_head.device
    anchors = anchors.to(device).type(all_cls_head.type())
    num_anchors = anchors.size()[0] if anchors is not None else 1
    num_classes = all_cls_head.size()[1] // num_anchors
    height, width = all_cls_head.size()[-2:]

    # Initialize output tensors
    batch_size = all_cls_head.size()[0]
    out_scores = torch.zeros((batch_size, top_n), device=device)
    out_boxes = torch.zeros((batch_size, top_n, num_boxes), device=device)
    out_classes = torch.zeros((batch_size, top_n), device=device)

    # Decode boxes for each item in the batch
    for batch in range(batch_size):
        cls_head = all_cls_head[batch, :, :, :].contiguous().view(-1)
        box_head = all_box_head[batch, :, :, :].contiguous().view(-1, num_boxes)

        # Filter out boxes based on score threshold
        keep = (cls_head >= threshold).nonzero().view(-1)
        if keep.nelement() == 0:
            continue

        # Select top scores
        scores = torch.index_select(cls_head, 0, keep)
        scores = scores.to(torch.float32)
        scores, indices = torch.topk(scores, min(top_n, keep.size()[0]), dim=0)
        scores = scores.to(torch.float16)
        indices = torch.index_select(keep, 0, indices).view(-1)
        classes = (indices / width / height) % num_classes
        classes = classes.type(all_cls_head.type())

        # Decode bounding boxes
        x = (indices % width).long()
        y = ((indices / width) % height).long()
        a = (indices / num_classes / height / width).long()
        box_head = box_head.view(num_anchors, num_boxes, height, width)
        boxes = box_head[a, :, y, x]

        # Adjust boxes based on anchors if provided
        if anchors is not None:
            grid = torch.stack([x, y, x, y], 1).type(all_cls_head.type()) * stride + anchors[a, :]
            boxes = _delta2box(boxes, grid, [width, height], stride)

        # Store results in output tensors
        out_scores[batch, :scores.size()[0]] = scores
        out_boxes[batch, :boxes.size()[0], :] = boxes
        out_classes[batch, :classes.size()[0]] = classes

    return out_scores, out_boxes, out_classes

# Define a preprocessing pipeline for the input image. 
# This includes converting the image to a tensor and normalizing its values.
preprocess = transforms.Compose([
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
])

def _detection_postprocess(image, cls_heads, box_heads):
    """Post-process detection outputs.

    This function takes the raw outputs from the detection model (class and box heads)
    and decodes them into human-readable bounding boxes with scores and labels.

    Args:
        image (torch.Tensor): The input image tensor.
        cls_heads (list of torch.Tensor): List of class head tensors.
        box_heads (list of torch.Tensor): List of box head tensors.

    Returns:
        tuple: Tuple containing:
            - scores (torch.Tensor): Scores of the detected boxes.
            - boxes (torch.Tensor): Detected bounding boxes.
            - labels (torch.Tensor): Class labels for the detected boxes.

    """
    
    # Dictionary to store anchors based on stride
    anchors = {}
    decoded = []
    
    # Loop over each class and box head to decode them
    for cls_head, box_head in zip(cls_heads, box_heads):
        # Calculate the stride based on the image and class head dimensions
        stride = image.shape[-1] // cls_head.shape[-1]
        
        # Generate anchors if they haven't been generated for this stride
        if stride not in anchors:
            anchors[stride] = _generate_anchors(stride, ratio_vals=[1.0, 2.0, 0.5],
                                               scales_vals=[4 * 2 ** (i / 3) for i in range(3)])
        
        # Decode the class and box heads into human-readable format
        decoded.append(_decode(torch.tensor(cls_head), torch.tensor(box_head), stride,
                              threshold=0.05, top_n=1000, anchors=anchors[stride]))
    
    # Concatenate results from all heads
    decoded = [torch.cat(tensors, 1) for tensors in zip(*decoded)]
    
    # Apply non-maximum suppression to remove overlapping boxes
    scores, boxes, labels = _nms(*decoded, nms=0.5, ndetections=6)
    
    return scores, boxes, labels

def largest_power_of_two(n):
    # Find the exponent of the largest power of 2 less than or equal to n
    exponent = math.floor(math.log2(n))
    # Return the largest power of 2
    return 2 ** exponent

def image_inference(input_image, scores_threshold, img_sensitivity):
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
    _proc_img_width =480
    _proc_img_height=288
    #num_threads=2
    speed_control=0.5
    bin_file="nozcam_test_q.bin"

    # Get the total number of CPU cores
    total_cpu_cores = multiprocessing.cpu_count()

    # Use half of the total CPU cores, or 1 if there's only 1 core
    num_threads_candidate = max(1, math.ceil(total_cpu_cores * speed_control))
    num_threads = largest_power_of_two(num_threads_candidate)

    # Get the width and height of the image
    img_width, img_height = input_image.size

    # Resize the image
    input_image = input_image.resize((_proc_img_width, _proc_img_height))
    
    input_tensor = preprocess(input_image)
    input_batch = input_tensor.unsqueeze(0)

    # Start the timer
    start_time = time.time()
    # Run the ONNX model inference
    sess_opt = onnxruntime.SessionOptions()
    sess_opt.intra_op_num_threads = num_threads
    ort_session = onnxruntime.InferenceSession(bin_file, sess_opt)

    ort_inputs = {ort_session.get_inputs()[0].name: np.array(input_batch)}
    ort_outs = ort_session.run(None, ort_inputs)

    # Stop the timer
    end_time = time.time()
    # Calculate the elapsed time
    elapsed_time = end_time - start_time
    

    # Split the output into classification and box regression heads
    cls_heads = ort_outs[:5]
    box_heads = ort_outs[5:10]

    scores, boxes, labels = _detection_postprocess(input_tensor, cls_heads, box_heads)

    # Convert to numpy
    scores = scores.numpy()
    boxes = boxes.numpy()
    labels = labels.numpy()

    # Create a 2D array (bitmap) representing the image
    bitmap = [[False for _ in range(_proc_img_height)] for _ in range(_proc_img_width)]
    
    # Filter boxes based on scores_threshold and label constraints
    filtered_boxes = [box for score, box, label in zip(scores[0], boxes[0], labels[0]) 
                    if score > scores_threshold and 0.5 < label < 1.5]
    

    for box in filtered_boxes:
        x1, y1, x2, y2 = map(int, box)
        # Clip the coordinates to stay within the image boundaries
        x1 = max(0, min(x1, _proc_img_height - 1))
        y1 = max(0, min(y1, _proc_img_width - 1))
        x2 = max(0, min(x2, _proc_img_height))
        y2 = max(0, min(y2, _proc_img_width))
        
        for i in range(y1, y2):
            for j in range(x1, x2):
                bitmap[i][j] = True

    # Count the number of ones in the bitmap
    total_area = sum(row.count(True) for row in bitmap)

    # Calculate the percentage of the total area covered by the boxes
    percentage_area = total_area / (_proc_img_width * _proc_img_height)
    
    # Divide by img_sensitivity
    severity = max(0, min(percentage_area / img_sensitivity, 1))
    severity = np.array([severity])

    # Calculate scaling factors
    height_scale = img_height / _proc_img_height
    width_scale = img_width / _proc_img_width

    # Scale the boxes to the original size of picture
    scaled_boxes = [[[x1 * width_scale, y1 * height_scale, x2 * width_scale, y2 * height_scale] for x1, y1, x2, y2 in box] for box in boxes]

    return scores, scaled_boxes, labels, severity, elapsed_time

