"""Render/read selected pages of an existing PDF; never rewrite its source."""
import json,hashlib
from pathlib import Path
import fitz
O=Path(__file__).resolve().parents[1];N=O.parents[1]
src=N/'deliverables/metasurface_review_v34/超表面光学成像综述_本地审阅稿.pdf'
dest=O/'evidence/publication/pages';dest.mkdir(parents=True,exist_ok=True)
before=hashlib.sha256(src.read_bytes()).hexdigest()
pages=[]
with fitz.open(src) as doc:
 for number in [40,41,47,51]:
  page=doc[number-1]
  image=dest/f'page_{number:03d}.png'
  page.get_pixmap(matrix=fitz.Matrix(1.25,1.25),alpha=False).save(image)
  pages.append({'page_1_based':number,'extracted_text':page.get_text(),'image':image.name,'image_sha256':hashlib.sha256(image.read_bytes()).hexdigest()})
result={'source':'N/deliverables/metasurface_review_v34/超表面光学成像综述_本地审阅稿.pdf','source_sha256':before,'page_count':66,'method':'Read-only text extraction and rasterization at 90dpi (1.25x), 1-based page numbers; source PDF not edited; no recompile','pages':pages}
(dest/'PDF_PAGE_EXCERPTS.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
assert before==hashlib.sha256(src.read_bytes()).hexdigest()
print(json.dumps({'selected_pages':[p['page_1_based'] for p in pages],'source_sha256':before,'source_unchanged':True,'output':'evidence/publication/pages'},ensure_ascii=False))
