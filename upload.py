import os
import cloudinary
cloudinary.config(cloud_url=os.getenv("CLOUDINARY_URL"))

import cloudinary.uploader

def upload_file(file_path, folder="uploads", file_name=None):
  """
  Uploads a file to Cloudinary and returns the upload response.
  """
  try:
    response = cloudinary.uploader.upload(
      file_path,
      folder=folder,  # optional: organizes uploads in a folder
      public_id=file_name,
      overwrite=True,  # overwrite if the file already exists
      resource_type="auto"  # auto-detect (image, raw, video, etc.)
    )
    return response.get("secure_url")
  except Exception as e:
    print(f"Upload failed: {e}")
    return None