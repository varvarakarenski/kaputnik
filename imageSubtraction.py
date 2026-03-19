from PIL import Image, ImageChops
import numpy as np

def subtract_images(image_path1: str, image_path2: str, output_path: str = "result.png") -> Image.Image:
    """
    Subtracts two images and returns a grayscale difference image.
    
    Args:
        image_path1: Path to the first image
        image_path2: Path to the second image
        output_path: Path to save the result (default: 'result.png')
    
    Returns:
        A grayscale PIL Image of the absolute difference
    """
    img1 = Image.open(image_path1).convert("RGB")
    img2 = Image.open(image_path2).convert("RGB")

    # Resize img2 to match img1 if dimensions differ
    if img1.size != img2.size:
        img2 = img2.resize(img1.size, Image.LANCZOS)

    # Subtract using numpy for absolute difference
    arr1 = np.array(img1, dtype=np.int16)
    arr2 = np.array(img2, dtype=np.int16)
    diff = np.abs(arr1 - arr2).astype(np.uint8)

    # Convert diff to grayscale
    diff_image = Image.fromarray(diff, mode="RGB").convert("L")
    diff_image.save(output_path)

    return diff_image


# --- Example usage ---
if __name__ == "__main__":
    result = subtract_images("image1.jpg", "image2.jpg", "diff_output.png")
    result.show()
    print(f"Result size: {result.size}, Mode: {result.mode}")
