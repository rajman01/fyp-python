import os

import cloudinary
import cloudinary.uploader
import cloudinary.api

def upload_file(file_path, folder="uploads"):
  """
  Uploads a file to Cloudinary and returns the upload response.
  """
  try:
    response = cloudinary.uploader.upload(
      file_path,
      folder=folder,  # optional: organizes uploads in a folder
      resource_type="auto"  # auto-detect (image, raw, video, etc.)
    )
    return response.get("secure_url")
  except Exception as e:
    print(f"Upload failed: {e}")
    return None