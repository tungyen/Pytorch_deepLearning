import math
from typing import List, Tuple, Dict, Optional
import torch
from torch import nn, Tensor
import torchvision
from .image_list import ImageList

@torch.jit.unused
def _resize_image_onnx(image, self_min_size, self_max_size):
    # type: (Tensor, float, float) -> Tensor
    from torch.onnx import operators
    im_shape = operators.shape_as_tensor(image)[-2:]
    min_size = torch.min(im_shape).to(dtype=torch.float32)
    max_size = torch.max(im_shape).to(dtype=torch.float32)
    scale_factor = torch.min(self_min_size / min_size, self_max_size / max_size)

    image = torch.nn.functional.interpolate(
        image[None], scale_factor=scale_factor, mode="bilinear", recompute_scale_factor=True,
        align_corners=False)[0]

    return image

def _resize_image(image, self_min_size, self_max_size):
    # type: (Tensor, float, float) -> Tensor
    im_shape = torch.tensor(image.shape[-2:])
    min_size = float(torch.min(im_shape))    # Get the minimum number between height and width
    max_size = float(torch.max(im_shape))    # Get the maximum number between height and width
    scale_factor = self_min_size / min_size  # Get the ratio of image minimum size to the current minimum size

    if max_size * scale_factor > self_max_size:
        scale_factor = self_max_size / max_size  # Set up ratio as maximum size to image maximum size

    # image[None] can add a new dimension for batch [C, H, W] -> [1, C, H, W]
    # bilinear only support 4D Tensor
    image = torch.nn.functional.interpolate(
        image[None], scale_factor=scale_factor, mode="bilinear", recompute_scale_factor=True,
        align_corners=False)[0]
    return image

class GeneralizedRCNNTransform(nn.Module):
    """
    Performs input / target transformation before feeding the data to a GeneralizedRCNN
    model.

    The transformations it perform are:
        - input normalization (mean subtraction and std division)
        - input / target resizing to match min_size / max_size

    It returns a ImageList for the inputs, and a List[Dict[Tensor]] for the targets
    """

    def __init__(self, min_size, max_size, image_mean, image_std):
        super(GeneralizedRCNNTransform, self).__init__()
        if not isinstance(min_size, (list, tuple)):
            min_size = (min_size,)
        self.min_size = min_size      # Minimum size for the image
        self.max_size = max_size      # Maximum size for the image
        self.image_mean = image_mean  # Mean of the image when normalizing
        self.image_std = image_std    # Variance of the image when normalizing

    def normalize(self, image):
        dtype, device = image.dtype, image.device
        mean = torch.as_tensor(self.image_mean, dtype=dtype, device=device)
        std = torch.as_tensor(self.image_std, dtype=dtype, device=device)
        # [:, None, None]: shape [3] -> [3, 1, 1]
        return (image - mean[:, None, None]) / std[:, None, None]

    def torch_choice(self, k):
        # type: (List[int]) -> int
        """
        Implements `random.choice` via torch ops so it can be compiled with
        TorchScript. Remove if https://github.com/pytorch/pytorch/issues/25803
        is fixed.
        """
        index = int(torch.empty(1).uniform_(0., float(len(k))).item())
        return k[index]

    def resize(self, image, target):
        # type: (Tensor, Optional[Dict[str, Tensor]]) -> Tuple[Tensor, Optional[Dict[str, Tensor]]]
        
        # Shrinking the image to designated range, and corresponding to shrinked bboxes
        # Inputs:
        #     image: Input Images with shape (channel, height, width)
        #     target: Annotation of images
        # Outputs:
        #     image: Shrinked images
        #     target: Annotation of shrinked image
        h, w = image.shape[-2:]
        if self.training:
            size = float(self.torch_choice(self.min_size))  
        else:
            # FIXME assume for now that testing uses the largest scale
            size = float(self.min_size[-1]) # Setting minimum size of images

        if torchvision._is_tracing():
            image = _resize_image_onnx(image, size, float(self.max_size))
        else:
            image = _resize_image(image, size, float(self.max_size))

        if target is None:
            return image, target

        bbox = target["boxes"]
        bbox = resize_boxes(bbox, [h, w], image.shape[-2:])
        target["boxes"] = bbox
        return image, target

    # _onnx_batch_images() is an implementation of batch_images() that is supported by ONNX tracing.
    @torch.jit.unused
    def _onnx_batch_images(self, images, size_divisible=32):
        # type: (List[Tensor], int) -> Tensor
        max_size = []
        for i in range(images[0].dim()):
            max_size_i = torch.max(torch.stack([img.shape[i] for img in images]).to(torch.float32)).to(torch.int64)
            max_size.append(max_size_i)
        stride = size_divisible
        max_size[1] = (torch.ceil((max_size[1].to(torch.float32)) / stride) * stride).to(torch.int64)
        max_size[2] = (torch.ceil((max_size[2].to(torch.float32)) / stride) * stride).to(torch.int64)
        max_size = tuple(max_size)

        # work around for
        # pad_img[: img.shape[0], : img.shape[1], : img.shape[2]].copy_(img)
        # which is not yet supported in onnx
        padded_imgs = []
        for img in images:
            padding = [(s1 - s2) for s1, s2 in zip(max_size, tuple(img.shape))]
            padded_img = torch.nn.functional.pad(img, [0, padding[2], 0, padding[1], 0, padding[0]])
            padded_imgs.append(padded_img)

        return torch.stack(padded_imgs)

    def max_by_axis(self, the_list):
        # type: (List[List[int]]) -> List[int]
        maxes = the_list[0]
        for sublist in the_list[1:]:
            for index, item in enumerate(sublist):
                maxes[index] = max(maxes[index], item)
        return maxes

    def batch_images(self, images, size_divisible=32):
        # type: (List[Tensor], int) -> Tensor
        # Inputs:
        #     images: Input images
        #     size_divisible: The number that the height and width can be divisible
        # Outputs:
        #     batched_imgs: Packaged batch tensor data
        if torchvision._is_tracing():
            # batch_images() does not export well to ONNX
            # call _onnx_batch_images() instead
            return self._onnx_batch_images(images, size_divisible)

        # Computing the maximum channel, height, and width of all images
        max_size = self.max_by_axis([list(img.shape) for img in images])
        stride = float(size_divisible)
        # Modifying size to the multiplier of stride by ceiling
        max_size[1] = int(math.ceil(float(max_size[1]) / stride) * stride)
        max_size[2] = int(math.ceil(float(max_size[2]) / stride) * stride)

        # [batch, channel, height, width]
        batch_shape = [len(images)] + max_size
        batched_imgs = images[0].new_full(batch_shape, 0)
        for img, pad_img in zip(images, batched_imgs):
            # Copying each image to new batched_imgs, and attach to top_left corner to assure bboxes unchanged
            pad_img[:img.shape[0], :img.shape[1], :img.shape[2]].copy_(img)
        return batched_imgs

    def postprocess(self,
                    result,                # type: List[Dict[str, Tensor]]
                    image_shapes,          # type: List[Tuple[int, int]]
                    original_image_sizes   # type: List[Tuple[int, int]]
                    ):
        # type: (...) -> List[Dict[str, Tensor]]
        # Postprocessing the prediction, mainly restoring bboxes to original scale
        # Inputs:
        #     result: list(dict), prediction of network, len(result) == batch_size
        #     image_shapes: list(torch.Size), Size of shrinked image
        #     original_image_sizes: list(torch.Size), Size of original image
        if self.training:
            return result

        # Iterating each prediction of images, and restoring bboxes back to orginal scale
        for i, (pred, im_s, o_im_s) in enumerate(zip(result, image_shapes, original_image_sizes)):
            boxes = pred["boxes"]
            boxes = resize_boxes(boxes, im_s, o_im_s)
            result[i]["boxes"] = boxes
        return result

    def __repr__(self):
        format_string = self.__class__.__name__ + '('
        _indent = '\n    '
        format_string += "{0}Normalize(mean={1}, std={2})".format(_indent, self.image_mean, self.image_std)
        format_string += "{0}Resize(min_size={1}, max_size={2}, mode='bilinear')".format(_indent, self.min_size,
                                                                                         self.max_size)
        format_string += '\n)'
        return format_string

    def forward(self,
                images,       # type: List[Tensor]
                targets=None  # type: Optional[List[Dict[str, Tensor]]]
                ):
        # type: (...) -> Tuple[ImageList, Optional[List[Dict[str, Tensor]]]]
        images = [img for img in images]
        for i in range(len(images)):
            image = images[i]
            target_index = targets[i] if targets is not None else None

            if image.dim() != 3:
                raise ValueError("images is expected to be a list of 3d tensors "
                                 "of shape [C, H, W], got {}".format(image.shape))
            image = self.normalize(image)
            image, target_index = self.resize(image, target_index)
            images[i] = image
            if targets is not None and target_index is not None:
                targets[i] = target_index

        # Recording the image size after resizing
        image_sizes = [img.shape[-2:] for img in images]
        images = self.batch_images(images)  # Packaging images into a batch
        image_sizes_list = torch.jit.annotate(List[Tuple[int, int]], [])

        for image_size in image_sizes:
            assert len(image_size) == 2
            image_sizes_list.append((image_size[0], image_size[1]))

        image_list = ImageList(images, image_sizes_list)
        return image_list, targets


def resize_boxes(boxes, original_size, new_size):
    # type: (Tensor, List[int], List[int]) -> Tensor
    # Inputs:
    #     original_size: Original image size
    #     new_size: Shrinked image size 
    ratios = [
        torch.tensor(s, dtype=torch.float32, device=boxes.device) /
        torch.tensor(s_orig, dtype=torch.float32, device=boxes.device)
        for s, s_orig in zip(new_size, original_size)
    ]
    ratios_height, ratios_width = ratios
    # Removes a tensor dimension, boxes [minibatch, 4]
    xmin, ymin, xmax, ymax = boxes.unbind(1)
    xmin = xmin * ratios_width
    xmax = xmax * ratios_width
    ymin = ymin * ratios_height
    ymax = ymax * ratios_height
    return torch.stack((xmin, ymin, xmax, ymax), dim=1)








