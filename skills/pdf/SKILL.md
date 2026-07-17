---
name: pdf
description: PDF file processing tools and approaches
---

# PDF Skill

## Available Approaches

### 1. Extract text from PDF

Use Python libraries like `PyMuPDF` (fitz), `pdfplumber`, or `pypdf`:

```python
import fitz
doc = fitz.open("file.pdf")
text = "\n".join(page.get_text() for page in doc)
```

### 2. Convert PDF to images

Use `pdf2image` for page-by-page image conversion, then OCR with `pytesseract`.

### 3. Metadata extraction

Use `pypdf` to read metadata (author, title, page count).

## Installation

```bash
uv add pymupdf pdfplumber pypdf
```
