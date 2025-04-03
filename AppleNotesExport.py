# -*- coding: utf-8 -*-
import os
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
import zlib
import traceback # For better error reporting
import shutil # For copying files
import re # For finding placeholders
import mimetypes # For guessing extension from UTI
import argparse
import tempfile # <-- Import tempfile for temporary file handling

# --- Tiktoken Import ---
try:
    import tiktoken
    # THIS global variable tracks if the import succeeded
    TIKTOKEN_IMPORTED_SUCCESSFULLY = True
except ImportError:
    TIKTOKEN_IMPORTED_SUCCESSFULLY = False
    print("Warning: 'tiktoken' library not found. Token count will not be calculated.")
    print("Install it via: pip install tiktoken")
# --- End Tiktoken Import ---


# Import the generated Protobuf code
try:
    import apple_notes_pb2
except ImportError:
    print("Error: Could not import apple_notes_pb2.py.")
    print("Please ensure you have run 'pip install protobuf' and compiled the .proto file with:")
    print("protoc --python_out=. apple_notes.proto")
    exit(1)
# Ignore the specific UserWarning about protobuf versions if it's just noise
import warnings
from google.protobuf import runtime_version
warnings.filterwarnings("ignore", category=UserWarning, module="google.protobuf.runtime_version")


# --- Configuration ---
NOTES_DATA_PATH = Path(os.path.expanduser("~/Library/Group Containers/group.com.apple.notes/"))
DB_PATH_DEFAULT = NOTES_DATA_PATH / "NoteStore.sqlite"
EXPORT_DIR_DEFAULT = Path("exported_notes") # Default export dir
ATTACHMENTS_SUBDIR = "_attachments"
LLM_OUTPUT_FILENAME = "llm_export.txt" # Default filename for LLM mode
TIKTOKEN_ENCODING = "cl100k_base" # Encoding for OpenAI models like GPT-3.5/4

# Apple Core Data timestamp starts from Jan 1, 2001
CORE_DATA_EPOCH = datetime(2001, 1, 1)

# Default constants (will be overridden if Z_PRIMARYKEY exists)
DEFAULT_Z_ENT_NOTE = 10; DEFAULT_Z_ENT_FOLDER = 5; DEFAULT_Z_ENT_ATTACHMENT = 7
DEFAULT_Z_ENT_MEDIA = 8; DEFAULT_Z_ENT_ACCOUNT = 1
DEFAULT_FOLDER_TYPE_TRASH = 1; DEFAULT_FOLDER_TYPE_SMART = 3

# --- Caches and Global State ---
account_uuid_cache = {}; folder_owner_cache = {}; folder_type_cache = {}; note_owner_cache = {}

# --- UTI to Extension Mapping ---
UTI_EXTENSIONS = {
    'public.jpeg': '.jpg', 'public.png': '.png', 'public.gif': '.gif',
    'public.tiff': '.tiff', 'com.adobe.pdf': '.pdf', 'public.plain-text': '.txt',
    'public.rtf': '.rtf', 'public.url': '.url', 'public.vcard': '.vcf',
    'com.apple.keynote.key': '.key', 'com.apple.keynote.kth': '.kth',
    'com.apple.numbers.numbers': '.numbers', 'com.apple.pages.pages': '.pages',
    'com.microsoft.word.doc': '.doc', 'org.openxmlformats.wordprocessingml.document': '.docx',
    'com.microsoft.excel.xls': '.xls', 'org.openxmlformats.spreadsheetml.sheet': '.xlsx',
    'com.microsoft.powerpoint.ppt': '.ppt', 'org.openxmlformats.presentationml.presentation': '.pptx',
    'public.mpeg-4': '.mp4', 'public.mpeg-4-audio': '.m4a', 'public.mp3': '.mp3',
    'com.apple.quicktime-movie': '.mov', 'com.apple.drawing': '.png',
    'com.apple.drawing.2': '.png', 'com.apple.paper': '.png',
    'com.apple.paper.doc.scan': '.pdf', 'com.apple.notes.gallery': '.jpg',
}

# --- Helper Functions ---
def get_entity_ids(cursor):
    entity_ids = {'ICNote': 10, 'ICFolder': 5, 'ICAttachment': 7, 'ICMedia': 8, 'ICAccount': 1}
    try:
        cursor.execute("SELECT Z_NAME, Z_ENT FROM Z_PRIMARYKEY")
        for name, ent_id in cursor.fetchall():
            if name in entity_ids: print(f"Dynamically found Z_ENT for {name}: {ent_id}"); entity_ids[name] = ent_id
        return entity_ids
    except sqlite3.OperationalError: return entity_ids

def convert_apple_timestamp(ts):
    if ts is None or ts == 0: return None
    try: return CORE_DATA_EPOCH + timedelta(seconds=ts)
    except TypeError: return None

def connect_db(db_path):
    if not db_path.exists(): raise FileNotFoundError(f"DB not found: {db_path}")
    db_uri = f"file:{db_path}?mode=ro";
    try: return sqlite3.connect(db_uri, uri=True)
    except sqlite3.OperationalError as e: print(f"Error connecting: {e}"); exit(1)

def sanitize_filename(name, allow_slashes=False):
    if not isinstance(name, str): name = "Untitled"
    allowed = (" ", "_", "-") + (("/",) if allow_slashes else ());
    sanitized = "".join(c for c in name if c.isalnum() or c in allowed)
    return ("_".join(sanitized.split())[:150].strip() or "Untitled")

# --- Database Query & Logic Functions ---
def get_folder_info(cursor, folder_pk, z_ent_folder):
    if folder_pk is None: return None, None, None
    folder_type = folder_type_cache.get(folder_pk); owner_pk, parent_pk = None, None
    query = "SELECT ZOWNER, ZPARENT, ZFOLDERTYPE FROM ZICCLOUDSYNCINGOBJECT WHERE Z_PK = ? AND Z_ENT = ?"
    try:
        cursor.execute(query, (folder_pk, z_ent_folder))
        result = cursor.fetchone()
        if result: owner_pk, parent_pk, db_type = result; folder_type_cache.setdefault(folder_pk, db_type); folder_type = db_type
        else: folder_type_cache[folder_pk] = None; return None, None, None
    except sqlite3.OperationalError as e: print(f"  Error folder PK {folder_pk}: {e}"); return None, None, folder_type
    return owner_pk, parent_pk, folder_type

def resolve_folder_owner(cursor, folder_pk, z_ent_folder):
    if folder_pk is None: return None
    if folder_pk in folder_owner_cache: return folder_owner_cache[folder_pk]
    owner_pk, parent_pk, _ = get_folder_info(cursor, folder_pk, z_ent_folder)
    final_owner = owner_pk if owner_pk is not None else (resolve_folder_owner(cursor, parent_pk, z_ent_folder) if parent_pk is not None else None)
    folder_owner_cache[folder_pk] = final_owner; return final_owner

def get_account_uuid(cursor, owner_pk, z_ent_account):
    if owner_pk is None: return None
    if owner_pk in account_uuid_cache: return account_uuid_cache[owner_pk]
    query = "SELECT ZIDENTIFIER FROM ZICCLOUDSYNCINGOBJECT WHERE Z_PK = ? AND Z_ENT = ?"
    try:
        cursor.execute(query, (owner_pk, z_ent_account))
        uuid = (res[0] if (res := cursor.fetchone()) else None); account_uuid_cache[owner_pk] = uuid; return uuid
    except sqlite3.OperationalError as e: print(f"  Error query account UUID PK {owner_pk}: {e}"); return None

def get_notes_and_owners(cursor, z_ent_note, z_ent_folder):
    notes = []; query_notes = "SELECT Z_PK, ZTITLE1, ZSNIPPET, ZCREATIONDATE1, ZMODIFICATIONDATE1, ZFOLDER, ZNOTEDATA FROM ZICCLOUDSYNCINGOBJECT WHERE Z_ENT = ? ORDER BY ZMODIFICATIONDATE1 DESC"
    query_data = "SELECT ZDATA FROM ZICNOTEDATA WHERE Z_PK = ?"
    try:
        all_notes = cursor.execute(query_notes, (z_ent_note,)).fetchall(); print(f"Retrieved {len(all_notes)} potential notes (pre-filtering).")
        skips = {'trash': 0, 'smart': 0, 'no_data': 0, 'no_folder': 0}
        for pk, title, snip, cr, mod, f_pk, nd_pk in all_notes:
            if nd_pk is None: skips['no_data'] += 1; continue
            if f_pk is None: skips['no_folder'] += 1; continue
            _, _, f_type = get_folder_info(cursor, f_pk, z_ent_folder)
            if f_type == DEFAULT_FOLDER_TYPE_SMART: skips['smart'] += 1; continue
            if f_type == DEFAULT_FOLDER_TYPE_TRASH: skips['trash'] += 1; continue
            if f_type is None: skips['no_folder'] += 1; continue
            owner_pk = resolve_folder_owner(cursor, f_pk, z_ent_folder); note_owner_cache[pk] = owner_pk
            blob = (res[0] if (res := cursor.execute(query_data, (nd_pk,)).fetchone()) else None)
            if blob: notes.append((pk, title, snip, cr, mod, owner_pk, blob))
            else: skips['no_data'] += 1
        print(f"Filtering complete: Skipped {skips['trash']} (Trash), {skips['smart']} (Smart), {skips['no_data']} (No Data), {skips['no_folder']} (No Folder/Error).")
    except sqlite3.OperationalError as e: print(f"Error initial notes query: {e}")
    return notes

def get_attachment_and_media_details(cursor, attach_id, z_att, z_med):
    q_att = "SELECT Z_PK, ZTYPEUTI, ZMEDIA FROM ZICCLOUDSYNCINGOBJECT WHERE ZIDENTIFIER = ? AND Z_ENT = ?"
    q_med = "SELECT ZIDENTIFIER, ZFILENAME, ZGENERATION1 FROM ZICCLOUDSYNCINGOBJECT WHERE Z_PK = ? AND Z_ENT = ?"
    att_pk, uti, med_pk, med_id, fname, gen = None, None, None, None, None, None
    try:
        att_pk, uti, med_pk = (res if (res := cursor.execute(q_att, (attach_id, z_att)).fetchone()) else (None, None, None))
        if med_pk is None: return att_pk, uti, None, None, None, None
        med_id, fname, gen = (res if (res := cursor.execute(q_med, (med_pk, z_med)).fetchone()) else (None, None, None))
        return att_pk, uti, med_pk, med_id, fname, gen
    except sqlite3.OperationalError as e:
        if "no such column: ZGENERATION1" in str(e):
             print(f"  Warning: ZGENERATION1 missing."); q_med_fb = "SELECT ZIDENTIFIER, ZFILENAME FROM ZICCLOUDSYNCINGOBJECT WHERE Z_PK = ? AND Z_ENT = ?"
             try: med_id, fname = (res if (res := cursor.execute(q_med_fb, (med_pk, z_med)).fetchone()) else (None, None)); return att_pk, uti, med_pk, med_id, fname, None
             except sqlite3.OperationalError as e2: print(f"  DB error (media fallback) PK {med_pk}: {e2}"); return att_pk, uti, med_pk, None, None, None
        else: print(f"  DB error lookup attach ID {attach_id}: {e}"); return None, None, None, None, None, None

def find_attachment_source_path(cursor, att_pk, uti, med_id, fname, gen, z_att, acc_uuid):
    paths = ([NOTES_DATA_PATH / "Accounts" / acc_uuid] if acc_uuid else []) + [NOTES_DATA_PATH]
    for base in paths:
        if med_id and fname: # Standard Media
            g = str(gen) if gen else ''; p = base / "Media" / med_id / g / fname;
            if p.exists(): return p
            if g: p_no_g = base / "Media" / med_id / fname;
            if g and p_no_g.exists(): return p_no_g
        if uti in ["com.apple.drawing", "com.apple.drawing.2", "com.apple.paper"]: # Drawings
            q = "SELECT ZIDENTIFIER, ZFALLBACKIMAGEGENERATION FROM ZICCLOUDSYNCINGOBJECT WHERE Z_PK = ? AND Z_ENT = ?"
            try: a_uuid, fb_g = (res if (res := cursor.execute(q, (att_pk, z_att)).fetchone()) else (None, None))
            except sqlite3.OperationalError: a_uuid, fb_g = None, None
            if a_uuid:
                if fb_g: p = base / "FallbackImages" / a_uuid / fb_g / "FallbackImage.png";
                if fb_g and p.exists(): return p
                for ext in ["jpg", "png"]: p = base / "FallbackImages" / f"{a_uuid}.{ext}";
                if p.exists(): return p
        if uti == "com.apple.paper.doc.scan": # Modified Scan PDF
             q = "SELECT ZIDENTIFIER, ZFALLBACKPDFGENERATION FROM ZICCLOUDSYNCINGOBJECT WHERE Z_PK = ? AND Z_ENT = ?"
             try: a_uuid, fb_g = (res if (res := cursor.execute(q, (att_pk, z_att)).fetchone()) else (None, None))
             except sqlite3.OperationalError: a_uuid, fb_g = None, None
             if a_uuid: p = base / "FallbackPDFs" / a_uuid / (fb_g or '') / "FallbackPDF.pdf";
             if a_uuid and p.exists(): return p
        if uti == "com.apple.notes.gallery": # Scan Gallery Preview
            q = "SELECT ZIDENTIFIER, ZSIZEWIDTH, ZSIZEHEIGHT FROM ZICCLOUDSYNCINGOBJECT WHERE Z_PK = ? AND Z_ENT = ?"
            try: a_uuid, w, h = (res if (res := cursor.execute(q, (att_pk, z_att)).fetchone()) else (None, None, None))
            except sqlite3.OperationalError: a_uuid, w, h = None, None, None
            if a_uuid and w and h: p = base / "Previews" / f"{a_uuid}-1-{w}x{h}-0.jpeg";
            if a_uuid and w and h and p.exists(): return p
    return None

# --- Core Logic Functions ---
def decompress_gzip_data(gzipped_data):
    try: return zlib.decompress(gzipped_data, wbits=16 + zlib.MAX_WBITS)
    except zlib.error:
        try: return zlib.decompress(gzipped_data)
        except zlib.error: return None
    except Exception: return None

def decode_note_protobuf(data_blob):
    if not data_blob: return "[No data blob]"
    decompressed = decompress_gzip_data(data_blob);
    if not decompressed: return "[Decompression failed]"
    try:
        proto = apple_notes_pb2.NoteStoreProto(); proto.ParseFromString(decompressed)
        if proto.HasField('document') and proto.document.HasField('note'):
            note = proto.document.note; text, pos = "", 0; content = note.noteText or ""
            if hasattr(note, 'attributeRun') and note.attributeRun:
                for run in note.attributeRun:
                    l = run.length; seg = content[pos : pos + l]
                    if run.HasField('attachmentInfo'): text += f"![ATTACHMENT|{run.attachmentInfo.attachmentIdentifier}|{run.attachmentInfo.typeUti}]"
                    else: text += seg
                    pos += l
            else: text = content
            return text
        else: return "[Doc/Note not found in Proto]"
    except Exception as e: print(f"  [Proto Decode Error: {e}]"); return f"[Proto decode error]"

def decode_note_protobuf_text_only(data_blob):
    if not data_blob: return "[No data blob]"
    decompressed = decompress_gzip_data(data_blob);
    if not decompressed: return "[Decompression failed]"
    try:
        proto = apple_notes_pb2.NoteStoreProto(); proto.ParseFromString(decompressed)
        if proto.HasField('document') and proto.document.HasField('note'):
            note = proto.document.note; text, pos = "", 0; content = note.noteText or ""
            if hasattr(note, 'attributeRun') and note.attributeRun:
                for run in note.attributeRun:
                    l = run.length; seg = content[pos : pos + l]
                    if not run.HasField('attachmentInfo'): text += seg # Only text
                    pos += l
            else: text = content
            return text.replace('\ufffc', '').strip() # Clean and strip
        else: return "[Doc/Note not found in Proto]"
    except Exception as e: print(f"  [Proto Decode Text Error: {e}]"); return f"[Proto decode error]"

def get_extension_from_uti(uti, fallback_filename=None):
    if uti in UTI_EXTENSIONS: return UTI_EXTENSIONS[uti]
    ext = mimetypes.guess_extension(uti)
    if ext: return ext
    if fallback_filename: return Path(fallback_filename).suffix
    return ".bin"

def process_attachments(text_placeholders, cursor, note_pk, owner_pk, base_path, ids):
    regex = re.compile(r'!\[ATTACHMENT\|([^|]+)\|([^\]]+)\]'); processed = text_placeholders
    processed_cache = {}; att_dir = base_path / ATTACHMENTS_SUBDIR; att_dir.mkdir(exist_ok=True)
    non_file = ["com.apple.notes.table", "com.apple.notes.inlinetextattachment.hashtag", "com.apple.notes.inlinetextattachment.mention", "com.apple.notes.inlinetextattachment.link", "public.url"]
    img = ['image', 'jpeg', 'png', 'gif', 'tiff', 'scan', 'drawing', 'gallery', 'public.jpeg', 'public.png', 'public.tiff', 'public.gif', 'com.apple.drawing', 'com.apple.drawing.2', 'com.apple.paper']
    pdf = ['pdf', 'com.adobe.pdf', 'com.apple.paper.doc.scan']
    acc_uuid = get_account_uuid(cursor, owner_pk, ids['ICAccount']); offset = 0
    while True:
        m = regex.search(processed, offset)
        if not m: break
        holder, att_id, uti_ph = m.group(0), m.group(1), m.group(2); repl = f"[Att Error: {att_id}]"
        if uti_ph in non_file: repl = f"[Unsupported: {uti_ph}]"
        elif att_id in processed_cache: _, repl = processed_cache[att_id]
        else:
            att_pk, uti_db, _, med_id, fname, gen = get_attachment_and_media_details(cursor, att_id, ids['ICAttachment'], ids['ICMedia'])
            eff_uti = uti_db or uti_ph
            if not att_pk: print(f"  Skip: DB record missing ID {att_id} (UTI: {eff_uti})"); repl = f"[Att DB missing: {att_id}]"
            else:
                src = find_attachment_source_path(cursor, att_pk, eff_uti, med_id, fname, gen, ids['ICAttachment'], acc_uuid)
                if src:
                    base_fname = fname or src.name; stem = Path(base_fname).stem; ext = get_extension_from_uti(eff_uti, base_fname)
                    safe_stem = sanitize_filename(stem); dest_fname = f"{safe_stem}_{att_pk}{ext}"; dest_path = att_dir / dest_fname
                    try:
                        shutil.copy2(src, dest_path); rel_path = Path(ATTACHMENTS_SUBDIR) / dest_fname; alt = safe_stem.replace('_', ' ')
                        is_img = any(i in eff_uti.lower() for i in img); is_pdf = any(p in eff_uti.lower() for p in pdf)
                        if is_img: repl = f"![{alt}]({rel_path})"
                        elif is_pdf: repl = f"[{alt} (PDF)]({rel_path})"
                        else: repl = f"[{alt} (File)]({rel_path})"
                        processed_cache[att_id] = (dest_fname, repl)
                    except Exception as e: print(f"  Error copy {base_fname} from {src}: {e}"); repl = f"[Error copy {base_fname}]"
                else: repl = f"[Att source missing: {fname or att_id}]"; processed_cache[att_id] = (None, repl)
        processed = processed[:m.start()] + repl + processed[m.end():]; offset = m.start() + len(repl)
    return processed

def export_note_to_markdown(export_dir, pk, title, summary, created_ts, modified_ts, owner_pk, content_ph, cursor, ids):
    if not title:
        # Try to get title from first non-attachment line
        first_line = None
        if isinstance(content_ph, str) and content_ph.strip():
            potential_title = content_ph.strip().split('\n', 1)[0].strip()
            if not potential_title.startswith("![ATTACHMENT"):
                first_line = potential_title[:80]
        title = first_line if first_line else (summary[:80].strip() if summary else f"Untitled_Note_{pk}")

    safe_title = sanitize_filename(title); fpath = export_dir / f"{safe_title}_{pk}.md"
    final = process_attachments(content_ph, cursor, pk, owner_pk, export_dir, ids)
    cr_dt, md_dt = convert_apple_timestamp(created_ts), convert_apple_timestamp(modified_ts)
    try:
        with open(fpath, 'w', encoding='utf-8') as f:
            f.write(f"# {title}\n\n"); meta = []
            if cr_dt: meta.append(f"**Created:** {cr_dt:%Y-%m-%d %H:%M:%S}")
            if md_dt: meta.append(f"**Modified:** {md_dt:%Y-%m-%d %H:%M:%S}")
            if meta: f.write("\n".join(meta) + "\n\n---\n\n")
            f.write(final.replace('\ufffc', '') + "\n")
    except IOError as e: print(f"  [Error write {fpath.name}: {e}]")
    except Exception as e: print(f"  [Unexpected error write {fpath.name}: {e}]")

def append_note_to_llm_file(file_handle, pk, title, summary, modified_ts, text_content):
    if not title:
        first_line = None
        if isinstance(text_content, str) and text_content.strip():
            first_line = text_content.strip().split('\n', 1)[0].strip()[:80]
        title = first_line if first_line else (summary[:80].strip() if summary else f"Untitled_Note_{pk}")

    md_dt = convert_apple_timestamp(modified_ts); md_str = md_dt.strftime('%Y-%m-%d %H:%M:%S') if md_dt else "Unknown Date"
    try:
        file_handle.write(f"--- NOTE START ---\n")
        file_handle.write(f"Title: {title}\n")
        file_handle.write(f"Modified: {md_str}\n")
        file_handle.write(f"Content:\n{text_content}\n") # Assumes text_content is already clean
        file_handle.write(f"--- NOTE END ---\n\n")
    except IOError as e: print(f"  [Error writing note PK {pk} to LLM file: {e}]")

# --- Main Execution ---
def main():
    parser = argparse.ArgumentParser(description="Export Apple Notes.")
    parser.add_argument("--llm-output", action="store_true", help="LLM mode: Export text to single file.")
    parser.add_argument("-o", "--output-dir", type=Path, default=EXPORT_DIR_DEFAULT, help=f"Output directory for Markdown (default: {EXPORT_DIR_DEFAULT})")
    parser.add_argument("--llm-file", type=Path, default=LLM_OUTPUT_FILENAME, help=f"Output filename for LLM mode (default: {LLM_OUTPUT_FILENAME})")
    args = parser.parse_args()

    db_path = DB_PATH_DEFAULT; export_dir = args.output_dir; llm_file = args.llm_file
    print(f"Using database: {db_path}")
    if args.llm_output: print(f"LLM Mode: Exporting text to: {llm_file.resolve()}")
    else: print(f"Markdown Mode: Exporting notes to: {export_dir.resolve()}"); export_dir.mkdir(parents=True, exist_ok=True)

    conn = None; temp_llm_file = None
    try:
        conn = connect_db(db_path); cursor = conn.cursor(); ids = get_entity_ids(cursor); print(f"Using Entity IDs: {ids}")
        notes = get_notes_and_owners(cursor, ids['ICNote'], ids['ICFolder']); print(f"Found {len(notes)} notes to process.")
        if not notes: print("No valid notes found."); return

        exported_count = 0; print("Starting export...")

        if args.llm_output:
            # --- LLM Mode ---
            total_tokens = 0
            encoding = None
            # THIS local variable tracks if encoding loaded successfully *in this run*
            encoding_loaded_successfully = False

            # Check the global import status first
            if TIKTOKEN_IMPORTED_SUCCESSFULLY:
                try:
                    encoding = tiktoken.get_encoding(TIKTOKEN_ENCODING)
                    encoding_loaded_successfully = True # Set local flag on success
                    print(f"  Using tiktoken encoding: {TIKTOKEN_ENCODING}")
                except Exception as e:
                    print(f"Warning: Could not load tiktoken encoding '{TIKTOKEN_ENCODING}'. Token count disabled. Error: {e}")
                    # Keep encoding_loaded_successfully as False
            else:
                 print("Skipping token counting as tiktoken library is not available.")


            # Create a temporary file to store intermediate content
            temp_llm_fd, temp_llm_path_str = tempfile.mkstemp(suffix=".txt", text=True)
            temp_llm_path = Path(temp_llm_path_str)
            print(f"  Writing notes to temporary file: {temp_llm_path}")

            try:
                with os.fdopen(temp_llm_fd, 'w', encoding='utf-8') as temp_f:
                    for idx, (pk, title, summary, _, modified, _, blob) in enumerate(notes):
                        text = decode_note_protobuf_text_only(blob)
                        append_note_to_llm_file(temp_f, pk, title, summary, modified, text)
                        exported_count += 1
                        # Optional: Progress indicator here if needed

                print(f"  Finished writing {exported_count} notes to temporary file.")

                # Count tokens if possible (using the local flag and checking encoding)
                if encoding_loaded_successfully and encoding:
                    print(f"  Counting tokens using '{TIKTOKEN_ENCODING}'...")
                    try:
                        with open(temp_llm_path, 'r', encoding='utf-8') as temp_f_read:
                             # Read in chunks to handle potentially large files
                             chunk_size = 1024 * 1024 # 1MB chunks
                             while True:
                                 chunk = temp_f_read.read(chunk_size)
                                 if not chunk: break
                                 total_tokens += len(encoding.encode(chunk))
                        print(f"  Total token count: {total_tokens}")
                    except Exception as e:
                        print(f"  Error counting tokens: {e}. Token count will be omitted.")
                        total_tokens = 0 # Reset if counting failed
                else:
                     total_tokens = 0 # Ensure it's 0 if tiktoken not available/failed

                # Write final output file with header
                print(f"  Writing final output file: {llm_file}")
                export_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S %Z')
                with open(llm_file, 'w', encoding='utf-8') as final_f, \
                     open(temp_llm_path, 'r', encoding='utf-8') as temp_f_read:
                    final_f.write(f"# Apple Notes Export for LLM\n")
                    final_f.write(f"# Exported on: {export_time}\n")
                    if total_tokens > 0:
                        final_f.write(f"# Total Tokens ({TIKTOKEN_ENCODING}): {total_tokens}\n")
                    final_f.write(f"\n---\n\n")
                    # Copy content efficiently
                    shutil.copyfileobj(temp_f_read, final_f)

            finally:
                # Clean up the temporary file
                print(f"  Cleaning up temporary file: {temp_llm_path}")
                temp_llm_path.unlink(missing_ok=True)
            # --- End LLM Mode ---

        else:
            # --- Markdown Mode ---
            for idx, (pk, title, summary, created, modified, owner_pk, blob) in enumerate(notes):
                content_ph = decode_note_protobuf(blob)
                export_note_to_markdown(export_dir, pk, title, summary, created, modified, owner_pk, content_ph, cursor, ids)
                exported_count += 1
                if (exported_count % 100 == 0) or (exported_count == len(notes)): print(f"  Processed {exported_count}/{len(notes)} notes...")
            # --- End Markdown Mode ---

        print(f"\nFinished exporting {exported_count} notes.")

    except FileNotFoundError as e: print(f"Error: {e}")
    except Exception as e: print(f"An unexpected error occurred: {e}"); traceback.print_exc()
    finally:
        if conn: conn.close(); print("Database connection closed.")


if __name__ == "__main__":
    main()
