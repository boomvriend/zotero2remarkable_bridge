# sync_functions.py
import logging
import os
import zipfile
import tempfile
import hashlib
from pprint import pprint

from pyzotero.zotero import Zotero

import rmapi_shim as rmapi
import remarks
from pathlib import Path
from shutil import rmtree
from time import sleep
from datetime import datetime

logger = logging.getLogger("zotero_rM_bridge.sync_functions")

def sync_to_rm(item, zot, webdav, folders):
    temp_path = Path(tempfile.gettempdir())
    item_id = item["key"]
    attachments = zot.children(item_id)
    modified = False
    logger.info(f"Syncing {len(attachments)} to reMarkable")
    for entry in attachments:
        if "contentType" in entry["data"] and entry["data"]["contentType"] == "application/pdf":
            attachment_id = attachments[attachments.index(entry)]["key"]
            attachment_name = zot.item(attachment_id)["data"]["filename"]
            logger.info(f"Processing {attachment_name}...")

            # Get actual file and repack it in reMarkable's file format
            file_name = temp_path / attachment_name
            if webdav:
                webdav_downloader(webdav, attachment_id, attachment_name, temp_path)
            else:
                zot.dump(attachment_id, path=temp_path)
            
            if file_name:
                if rmapi.upload_file(file_name, f"/Zotero/{folders['unread']}"):
                    modified = True #prevent change multiple times on parent item, in case of a zot.parent with multiple children 
                    os.remove(file_name)
                    logger.info(f"Uploaded {attachment_name} to reMarkable.")
                else:
                    logger.error(f"Failed to upload {attachment_name} to reMarkable.")
        else:
            logger.warning("Found attachment, but it's not a PDF, skipping...")
    
    if modified:
        zot.add_tags(item, "synced")
        zot.delete_tags("to_sync") #this function has no item specific counterpart???

def webdav_downloader(webdav, attachment_id, attachment_name, path):
    '''
    mimics zot.dump behavior for webdav
    '''
    file_path = (path / f"{attachment_id}.zip")
    webdav.download_sync(remote_path=f"{attachment_id}.zip", local_path=file_path)
    with zipfile.ZipFile(file_path) as zf:
        zf.extractall(path)
        zf.extractall(".")
    if (path / attachment_name ).is_file():
        file_path.unlink()

def download_from_rm(entity: str, folder: str) -> Path:
    temp_path = Path(tempfile.gettempdir())
    logger.info(f"Processing {entity}...")
    zip_name = f"{entity}.rmdoc"
    file_path = temp_path / zip_name
    unzip_path = temp_path / f"{entity}-unzipped"
    download = rmapi.download_file(f"{folder}{entity}", str(temp_path))
    if download:
        logger.info("File downloaded")
    else:
        logger.warning("Failed to download file")

    with zipfile.ZipFile(file_path, "r") as zf:
        zf.extractall(unzip_path)

    remarks.run_remarks(str(unzip_path), temp_path)
    logging.info("PDF rendered")
    pdf = (temp_path / f"{entity} _remarks.pdf")
    pdf = pdf.rename(pdf.with_stem(f"{entity}"))
    pdf_name = pdf.name

    logging.info("PDF written")
    file_path.unlink()
    rmtree(unzip_path)

    return Path(temp_path / pdf_name)

def get_md5(pdf) -> None | str:
    if pdf.is_file():
        with open(pdf, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    return None


def get_mtime() -> str:
    return datetime.now().strftime('%s')

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

def zotero_upload(pdf_path: Path, zot: Zotero, webdav):
    md_name = f"{pdf_path.stem} _obsidian.md"
    md_path = pdf_path.with_name(md_name)

    annotated_name = f"(Annotated) {pdf_path.stem}{pdf_path.suffix}"
    annotated_path = pdf_path.with_name(annotated_name)
    logging.info(f"Have an annotated PDF {str(pdf_path)} to upload")

    pdf_path.rename(str(annotated_path))

    for item in zot.items(tag="synced"): #loop over all times with synced tag in zotero
        item_id = item["key"]
        item_name = item.get("data", {}).get("title") or item_id
        uploaded = False
        for attachment in zot.children(item_id): #loop over all attachements of a zotero entry a.k.a item
            if attachment["data"].get("filename", "") == pdf_path.name:
                already_uploaded_annotation = len([1 for tag in attachment["data"].get('tags') if tag["tag"] == "annotated"]) > 0
                if not already_uploaded_annotation:
                    files_to_upload = [str(annotated_path)]
                    if md_path.exists():
                        files_to_upload.append(str(md_path))
                    #distinction between webdav and non webdav
                    if webdav:
                        upload = add_to_zotero(files_to_upload, item_id, zot, webdav)
                    else:
                        upload = zot.attachment_simple(files_to_upload, item_id)
                        
                    if upload.get("success") or upload.get('unchanged'):
                        zot.add_tags(attachment, "annotated")
                        logging.info(f"{pdf_path} attached to Zotero item '{item_name}'.")
                        return
                    else:
                        logging.warning(f"Reason for upload failure: {upload.get('failure')}")
                        return
                        
                elif already_uploaded_annotation:
                    logging.warning(f"Uploading {pdf_path} to Zotero item '{item_name}' failed.\nAlready uploaded annotation.")
                    return

    logging.warning(f"Have an annotated PDF '{annotated_name}' to upload, but cannot find the appropriate item in Zotero")


def add_to_zotero(pdf_names, item_id, zot, webdav):
    '''
    mimics zot.attachment_simple behavior for webdav
    returns dictionary with an key "succes" with True or False
    '''

    temp_path = Path(tempfile.gettempdir())
    result = {'success' : True, 'failure': "WebDAV couldn't upload it."}
    #prepare and add zotero item entries
    pdf_paths = [Path(pdf_name) for pdf_name in pdf_names] #we only ask for pdf names because then it is similar to zotero.attach_simple but we convert back here to path
    item_templates = [None] * len(pdf_names) 
    for i in range(len(pdf_paths)):
        # fill in the item template
        item_templates[i] = zot.item_template("attachment", "imported_file")
        item_templates[i]["title"] = pdf_paths[i].stem
        item_templates[i]["filename"] = pdf_paths[i].name
        item_templates[i]["md5"] = get_md5(pdf_paths[i])
        item_templates[i]["mtime"] = datetime.now().strftime('%s')
    
        #create a zotero item (Maybe overlap with zot.attachment_simple)
        create_attachment = zot.create_items([item_templates[i]], item_id)
        if create_attachment["success"]:
            key = create_attachment["success"]["0"]
        else:
            result['success'] = False
            logging.info("Failed to create attachment, aborting...")
            return result

        #create zip for webdav storage
        attachment_zip = temp_path / f"{key}.zip"
        with zipfile.ZipFile(attachment_zip, "w") as zf:
            zf.write(pdf_paths[i], arcname=pdf_paths[i].name)
        remote_attachment_zip = attachment_zip.name

        #upload it to webdav
        attachment_upload = webdav_uploader(webdav, remote_attachment_zip, attachment_zip)
        if attachment_upload:
            logging.info("Attachment upload successful, proceeding...")
        else:
            result['success'] = False
            logging.error("Failed uploading attachment, skipping...")

        #For the file to be properly recognized in Zotero, a propfile needs to be
        #uploaded to the same folder with the same ID. The content needs 
        #to match exactly Zotero's format.
        propfile_content = f'<properties version="1"><mtime>{item_templates[i]["mtime"]}</mtime><hash>{item_templates[i]["md5"]}</hash></properties>'
        propfile = temp_path / f"{key}.prop"
        with open(propfile, "w") as pf:
            pf.write(propfile_content)
        remote_propfile = f"{key}.prop"
        
        propfile_upload = webdav_uploader(webdav, remote_propfile, propfile)
        if propfile_upload:
            logging.info("Propfile upload successful, proceeding...")
        else:
            result['success'] = False
            logging.error("Propfile upload failed, skipping...")
        
        #clean up
        logging.info(f"{pdf_paths[i].name} uploaded to Zotero.")
        (pdf_paths[i]).unlink()
        (temp_path / attachment_zip).unlink()
        (temp_path / propfile).unlink()
    
    return result

def get_sync_status(zot):
    read_list = []
    for item in zot.items(tag="read"):
        for attachment in zot.children(item["key"]):
            if "contentType" in attachment["data"] and attachment["data"]["contentType"] == "application/pdf":
                read_list.append(attachment["data"]["filename"])

    return read_list
