import os
import zipfile
import yaml
import tempfile
import hashlib
import rmapi_shim as rmapi
from pathlib import Path
from shutil import rmtree
from pyzotero import zotero
from webdav3.client import Client as wdClient
from rmrl import render
from time import sleep
from datetime import datetime


def load_config(config_file):
    with open(config_file, "r") as stream:
        try:
            config_dict = yaml.safe_load(stream)
        except yaml.YAMLError as exc:
            print(exc)
    zot = zotero.Zotero(config_dict["LIBRARY_ID"], config_dict["LIBRARY_TYPE"], config_dict["API_KEY"])
    folders = {"unread": config_dict["UNREAD_FOLDER"], "read": config_dict["READ_FOLDER"]}
    if config_dict["USE_WEBDAV"] == "True":
        webdav_data = {
            "webdav_hostname": config_dict["WEBDAV_HOSTNAME"],
            "webdav_login": config_dict["WEBDAV_USER"],
            "webdav_password": config_dict["WEBDAV_PWD"],
            "webdav_timeout": 300
            }   
        webdav = wdClient(webdav_data)
    else:
        webdav = False
    return (zot, webdav, folders)
    

def write_config(file_name):
    config_data = {}
    input("Couldn't find config file. Let's create one! Press Enter to continue...")
    config_data["UNREAD_FOLDER"] = input("Which ReMarkable folder should files be synced to?")
    config_data["READ_FOLDER"] = input("Which ReMarkable folder should files be synced from?")
    config_data["LIBRARY_ID"] = input("Enter Zotero library ID: ")
    config_data["LIBRARY_TYPE"] = input("Enter Zotero library type (user/group): ")
    config_data["API_KEY"] = input("Enter Zotero API key: ")
    config_data["USE_WEBDAV"] = input("Does Zotero use WebDAV storage for file sync (True/False)? ")
    if config_data["USE_WEBDAV"] == "True":
        config_data["WEBDAV_HOSTNAME"] = input("Enter path to WebDAV folder (same as in Zotero config): ")
        config_data["WEBDAV_USER"] = input("Enter WebDAV username: ")
        config_data["WEBDAV_PWD"] = input("Enter WebDAV password (consider creating an app token as password is safed in clear text): ")
    
    with open(file_name, "w") as file:
        yaml.dump(config_data, file)
    print(f"Config written to {file_name}\n If something went wrong, please edit config manually.")


def sync_to_rm(item, zot, folders):
    temp_path = Path(tempfile.gettempdir())
    item_id = item["key"]
    attachments = zot.children(item_id)
    for entry in attachments:
        if "contentType" in entry["data"] and entry["data"]["contentType"] == "application/pdf":
            attachment_id = attachments[attachments.index(entry)]["key"]
            attachment_name = zot.item(attachment_id)["data"]["filename"]
            print(f"Processing {attachment_name}...")
                
            # Get actual file and repack it in reMarkable's file format
            file_name = zot.dump(attachment_id, path=temp_path)
            if file_name:
                upload = rmapi.upload_file(file_name, f"/Zotero/{folders['unread']}")
            if upload:
                zot.add_tags(item, "synced")
                os.remove(file_name)
                print(f"Uploaded {attachment_name} to reMarkable.")
            else:
                print(f"Failed to upload {attachment_name} to reMarkable.")
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
            unzip_path = Path(temp_path / "unzipped")     
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
    unzip_path = temp_path / "unzipped"
    download = rmapi.download_file(f"{folder}{entity}", str(temp_path))
    if download:
        print("File downloaded")
    else:
        print("Failed to download file")

    with zipfile.ZipFile(file_path, "r") as zf:
        zf.extractall(unzip_path)
    (unzip_path / f"{content_id}.pagedata").unlink()
    with zipfile.ZipFile(file_path, "w") as zf:
        for entry in sorted(unzip_path.glob("**/*")):
            zf.write(unzip_path / entry, arcname=entry)

    output = render(str(file_path))
    print("PDF rendered")
    pdf_name = f"{entity}.pdf"
    with open(temp_path / pdf_name, "wb") as outputFile:
        outputFile.write(output.read())
    print("PDF written")
    file_path.unlink()

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
