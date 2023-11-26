import os
import zipfile
import yaml
import tempfile
import hashlib
import rmapi_shim as rmapi
import remarks
from pathlib import Path
from shutil import rmtree
from pyzotero import zotero
from webdav3.client import Client as wdClient
from time import sleep
from datetime import datetime

class TempDir:
    def __init__(self):
        self.path = tempfile.mkdtemp()

    def __enter__(self):
        return self
    
    def __exit__(self, ex_type, ex_value, ex_traceback):
        self.delete()
    
    def delete(self):
        if os.path.exists(self.path):
            rmtree(self.path)
            
class TempFile:
    @property
    def path(self):
        return os.path.join(self.dir.path, self.file_name)
    
    @property
    def exists(self):
        return os.path.exists(self.path)
    
    def __init__(self, tempdir: TempDir, file_name: str):
        self.dir = tempdir
        self.file_name = file_name
        
    def __enter__(self):
        return self
    
    def __exit__(self, ex_type, ex_value, ex_traceback):
        self.delete()
    
    def delete(self):
        if os.path.exists(self.path):
            os.remove(self.path)
        
        


def sync_to_rm(item, zot, folders):
    # Create new temporary dir
    temp_dir = TempDir()
    
    # Get all item IDs and attachments, iterate through...
    item_id = item["key"]
    attachments = zot.children(item_id)
    for entry in attachments:
        if "contentType" in entry["data"] and entry["data"]["contentType"] == "application/pdf":
            attachment_id = attachments[attachments.index(entry)]["key"]
            attachment_name = zot.item(attachment_id)["data"]["filename"]
            print(f"Processing {attachment_name}...")
            
            # Get actual file and repack it in reMarkable's file format
            with TempFile(temp_dir, attachment_name) as tf:
                with open(tf.path, 'wb') as bf:
                    bf.write(zot.file(attachment_id))
                
                if tf.exists:
                    upload = rmapi.upload_file(tf.path, f"/Zotero/{folders['unread']}")
            
                    if upload:
                        zot.add_tags(item, "synced")
                        print(f"Uploaded {attachment_name} to reMarkable.")
                    else:
                        print(f"Failed to upload {attachment_name} to reMarkable.")
                else:
                    print(f"Failed to write {attachment_name} to disk")
        else:
            print("Found attachment, but it's not a PDF, skipping...")
    
       
def sync_to_rm_webdav(item, zot, webdav, folders):
    temp_path = Path(tempfile.gettempdir())
    item_id = item["key"]
    attachments = zot.children(item_id)
    for entry in attachments:
        if "contentType" in entry["data"] and entry["data"]["contentType"] == "application/pdf":
            attachment_id = attachments[attachments.index(entry)]["key"]
            attachment_name = zot.item(attachment_id)["data"]["filename"]
            print(f"Processing {attachment_name}...")
    
            # Get actual file from webdav, extract it and repack it in reMarkable's file format
            file_name = f"{attachment_id}.zip"
            file_path = Path(temp_path / file_name)
            unzip_path = Path(temp_path / f"{file_name}-unzipped")
            webdav.download_sync(remote_path=file_name, local_path=file_path)
            with zipfile.ZipFile(file_path) as zf:
                zf.extractall(unzip_path)
            if (unzip_path / attachment_name ).is_file():
                uploader = rmapi.upload_file(str(unzip_path / attachment_name), f"/Zotero/{folders['unread']}")
            else:
                """ #TODO: Sometimes Zotero does not seem to rename attachments properly,
                    leading to reported file names diverging from the actual one. 
                    To prevent this from stopping the whole program due to missing
                    file errors, skip that file. Probably it could be worked around better though.""" 
                print("PDF not found in downloaded file. Filename might be different. Try renaming file in Zotero, sync and try again.")
                break
            if uploader:
                zot.add_tags(item, "synced")
                file_path.unlink()
                rmtree(unzip_path)
                print(f"Uploaded {attachment_name} to reMarkable.")
            else:
                print(f"Failed to upload {attachment_name} to reMarkable.")
        else:
            print("Found attachment, but it's not a PDF, skipping...")


def download_from_rm(entity, folder, content_id):
    temp_path = Path(tempfile.gettempdir())
    print(f"Processing {entity}...")
    zip_name = f"{entity}.zip"
    file_path = temp_path / zip_name
    unzip_path = temp_path / f"{entity}-unzipped"
    download = rmapi.download_file(f"{folder}{entity}", str(temp_path))
    if download:
        print("File downloaded")
    else:
        print("Failed to download file")

    with zipfile.ZipFile(file_path, "r") as zf:
        zf.extractall(unzip_path)

    renderer = remarks
    args = {"combined_pdf": True, "combined_md": False, "ann_type": ["scribbles", "highlights"]}
    renderer.run_remarks(unzip_path, temp_path, **args)
    print("PDF rendered")
    pdf = (temp_path / f"{entity} _remarks.pdf")
    pdf = pdf.rename(pdf.with_stem(f"{entity}"))
    pdf_name = pdf.name

    print("PDF written")
    file_path.unlink()
    rmtree(unzip_path)

    return pdf_name


def zotero_upload(pdf_name, zot):
    for item in zot.items(tag="synced"):
        item_id = item["key"]
        for attachment in zot.children(item_id):
            if "filename" in attachment["data"] and attachment["data"]["filename"] == pdf_name:
                #zot.delete_item(attachment)
                # Keeping the original seems to be the more sensible thing to do
                new_pdf_name = pdf_name.with_stem(f"(Annot) {pdf_name.stem}")
                pdf_name.rename(new_pdf_name)
                upload = zot.attachment_simple([new_pdf_name], item_id)                
                
                if upload["success"] != []:
                    print(f"{pdf_name} uploaded to Zotero.")
                else:
                    print(f"Upload of {pdf_name} failed...")
                return


def get_md5(pdf):
    if pdf.is_file():
        with open(pdf, "rb") as f:
            bytes = f.read()
            md5 = hashlib.md5(bytes).hexdigest()
    else:
        md5 = None
    return md5


def get_mtime():
    mtime = datetime.now().strftime('%s')
    return mtime


def fill_template(item_template, pdf_name):
    item_template["title"] = pdf_name.stem
    item_template["filename"] = pdf_name.name
    item_template["md5"] = get_md5(pdf_name)
    item_template["mtime"] = get_mtime()
    return item_template


def webdav_uploader(webdav, remote_path, local_path):
    for i in range(3):
        try:
            webdav.upload_sync(remote_path=remote_path, local_path=local_path)
        except:
            sleep(5)
        else:
            return True
    else:
        return False


def zotero_upload_webdav(pdf_name, zot, webdav):
    temp_path = Path(tempfile.gettempdir())
    item_template = zot.item_template("attachment", "imported_file")
    for item in zot.items(tag=["synced", "-read"]):
        item_id = item["key"]
        for attachment in zot.children(item_id):
            if "filename" in attachment["data"] and attachment["data"]["filename"] == pdf_name:
                pdf_name = temp_path / pdf_name
                new_pdf_name = pdf_name.with_stem(f"(Annot) {pdf_name.stem}")
                pdf_name.rename(new_pdf_name)
                pdf_name = new_pdf_name
                filled_item_template = fill_template(item_template, pdf_name)
                create_attachment = zot.create_items([filled_item_template], item_id)
                
                if create_attachment["success"] != []:
                    key = create_attachment["success"]["0"]
                else:
                    print("Failed to create attachment, aborting...")
                    continue
                
                attachment_zip = temp_path / f"{key}.zip"
                with zipfile.ZipFile(attachment_zip, "w") as zf:
                    zf.write(pdf_name, arcname=pdf_name.name)
                remote_attachment_zip = attachment_zip.name
                
                attachment_upload = webdav_uploader(webdav, remote_attachment_zip, attachment_zip)
                if attachment_upload:
                    print("Attachment upload successfull, proceeding...")
                else:
                    print("Failed uploading attachment, skipping...")
                    continue

                """For the file to be properly recognized in Zotero, a propfile needs to be
                uploaded to the same folder with the same ID. The content needs 
                to match exactly Zotero's format."""
                propfile_content = f'<properties version="1"><mtime>{item_template["mtime"]}</mtime><hash>{item_template["md5"]}</hash></properties>'
                propfile = temp_path / f"{key}.prop"
                with open(propfile, "w") as pf:
                    pf.write(propfile_content)
                remote_propfile = f"{key}.prop"
                
                propfile_upload = webdav_uploader(webdav, remote_propfile, propfile)
                if propfile_upload:
                    print("Propfile upload successful, proceeding...")
                else:
                    print("Propfile upload failed, skipping...")
                    continue
                            
                zot.add_tags(item, "read")
                print(f"{pdf_name.name} uploaded to Zotero.")
                (temp_path / pdf_name).unlink()
                (temp_path / attachment_zip).unlink()
                (temp_path / propfile).unlink()
                return pdf_name
            

def get_sync_status(zot):
    read_list = []
    for item in zot.items(tag="read"):
        for attachment in zot.children(item["key"]):
            if "contentType" in attachment["data"] and attachment["data"]["contentType"] == "application/pdf":
                read_list.append(attachment["data"]["filename"])
    
    return read_list
