```markdown
# Apple Notes Export Script

This Python script exports notes from the Apple Notes application (`NoteStore.sqlite`) into more portable formats: individual Markdown files (with attachments) or a single consolidated text file suitable for ingestion by Large Language Models (LLMs).

## Features

*   **Markdown Export:** Exports each note into a separate `.md` file.
    *   Attempts to use the first line of the note or the note's summary as the filename.
    *   Includes creation and modification dates in the Markdown file header.
    *   Copies attachments (images, PDFs, other files) associated with notes to an `_attachments` subfolder.
    *   Replaces attachment references in the notes with relative Markdown links (`![Image](_attachments/...)` or `[File](_attachments/...)`).
*   **LLM Export Mode:** Exports all notes into a single `.txt` file (`--llm-output`).
    *   Includes the note title and last modified date for each note.
    *   Exports **only the text content** of the notes, stripping out attachments.
    *   Prepends a header to the file indicating the export time.
    *   Optionally calculates and includes the total token count using `tiktoken` (requires installation).
*   **Attachment Handling:** Attempts to locate attachment files, considering different potential locations (including account-specific paths and fallback locations for drawings/scans).
*   **Robustness:** Dynamically looks up internal Apple Notes entity IDs (like notes, folders, attachments) if possible, making it less dependent on specific hardcoded values.
*   **Filtering:** Skips notes located in the "Recently Deleted" folder and Smart Folders. Skips password-protected notes.

## Requirements

*   **Operating System:** macOS (requires access to the Apple Notes data directory)
*   **Python:** Python 3.8 or higher recommended.
*   **Apple Notes Data:** Access to the Apple Notes database directory (`~/Library/Group Containers/group.com.apple.notes/`).
*   **Protobuf Compiler (`protoc`):** Required to compile the `.proto` definition for Apple Notes data structures.
    *   Install via Homebrew: `brew install protobuf`
*   **Python Libraries:**
    *   `protobuf`: For handling the compiled Protobuf definitions.
        *   Install via pip: `pip install protobuf`
    *   `tiktoken` (Optional): For calculating token counts in LLM mode.
        *   Install via pip: `pip install tiktoken`

## Setup

1.  **Clone or Download:** Get the script files (`AppleNotesExport.py` and `apple_notes.proto`) into a local directory.
2.  **Install Prerequisites:** Ensure you have Python 3.8+, `protoc`, and the required Python libraries installed (see Requirements above).
3.  **Compile Protobuf Definition:** Navigate to the directory containing the script files in your terminal and run:
    ```bash
    protoc --python_out=. apple_notes.proto
    ```
    This command **must** be run successfully. It generates the `apple_notes_pb2.py` file, which the main script imports. If this file is missing, the script will fail.

## Usage

Run the script from your terminal in the directory where you placed the files.

**1. Default Markdown Export:**

Exports notes to a subfolder named `exported_notes` in the current directory.

```bash
python3 AppleNotesExport.py
```

**2. Markdown Export to a Specific Directory:**

```bash
python3 AppleNotesExport.py -o /path/to/your/desired/output/folder
```

**3. LLM Mode Export (Single Text File):**

Exports all note text content to `llm_export.txt` in the current directory. Includes token count if `tiktoken` is installed.

```bash
python3 AppleNotesExport.py --llm-output
```

**4. LLM Mode Export to a Specific File:**

```bash
python3 AppleNotesExport.py --llm-output --llm-file my_notes_corpus.txt
```

**5. Show Help:**

Displays all available command-line options.

```bash
python3 AppleNotesExport.py --help
```

## Output Formats

### Markdown Mode

*   Creates a directory (default: `exported_notes`).
*   Inside this directory:
    *   Individual note files named like `Note_Title_123.md` (where `123` is an internal ID).
    *   A subfolder named `_attachments` containing copies of all linked attachment files (images, PDFs, etc.), renamed for uniqueness (`original_filename_456.ext`).
*   Markdown File Structure:
    ```markdown
    # Note Title

    **Created:** 2023-10-27 10:00:00
    **Modified:** 2023-10-27 11:30:00

    ---

    This is the note content.

    Here is an image: ![My Drawing](_attachments/My_Drawing_789.png)

    Here is a PDF: [Report Q3 (PDF)](_attachments/Report_Q3_101.pdf)
    ```

### LLM Mode

*   Creates a single text file (default: `llm_export.txt`).
*   File Structure:
    ```txt
    # Apple Notes Export for LLM
    # Exported on: 2023-10-27 12:00:00 PDT
    # Total Tokens (cl100k_base): 15789

    ---

    --- NOTE START ---
    Title: Meeting Notes - Project Alpha
    Modified: 2023-10-26 14:25:10
    Content:
    Discussed milestones for Q4.
    Action items assigned.
    Next meeting scheduled.
    --- NOTE END ---

    --- NOTE START ---
    Title: Recipe Idea
    Modified: 2023-10-25 09:15:00
    Content:
    Ingredients: Flour, sugar, eggs...
    Instructions: Mix dry ingredients...
    --- NOTE END ---

    ... more notes ...
    ```
    *   The token count line only appears if `tiktoken` is installed and the calculation succeeds.

## Limitations & Caveats

*   **macOS Only:** The script directly accesses the Apple Notes database location specific to macOS.
*   **Encrypted Notes:** Password-protected notes cannot be decrypted and will be skipped.
*   **Formatting:** Conversion to Markdown is basic. Complex formatting (tables, checklists, rich links beyond simple URLs) may be lost or represented as plain text/placeholders. Tables are notably *not* supported currently.
*   **Attachment Location:** While the script tries various paths, it might fail to find attachment source files, especially on older macOS versions or if the `NoteStore.sqlite` database is out of sync with the file system data. Missing attachments will be noted in the output (`[Attachment source missing: ...]`).
*   **Database Updates:** Future updates to Apple Notes could change the database schema or the Protobuf structure, potentially breaking the script. The `.proto` file might need updating.
*   **Error Handling:** Basic error handling is included, but unexpected database states or file system issues might cause errors.

## Troubleshooting

*   **`ImportError: No module named 'apple_notes_pb2'`:** You forgot to run the `protoc --python_out=. apple_notes.proto` command in the script's directory.
*   **`FileNotFoundError: ... NoteStore.sqlite not found`:** Ensure Apple Notes is installed and the script has permissions to read `~/Library/Group Containers/group.com.apple.notes/`. The script copies the database to a temporary location first to avoid locking issues, but it still needs read access to the original.
*   **`tiktoken` Warning:** If you see a warning about `tiktoken` not being found, the LLM export will still work, but the token count header will be omitted. Install it (`pip install tiktoken`) if you need the count.
*   **Attachment Errors (`[Att Error...]`, `[Att DB missing...]`, `[Att source missing...]`):** This usually means the script couldn't find the record for the attachment in the database or couldn't locate the corresponding file on disk. This can happen if Notes data is slightly corrupt or files were manually moved/deleted.

## License

This script is provided as-is. You are free to use, modify, and distribute it. Please refer to the specific licenses of any third-party libraries used (like `protobuf` and `tiktoken`).
