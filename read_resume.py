import fitz
doc = fitz.open(r'C:\Users\Administrator\Desktop\Agent开发简历.pdf')
text = ''
for page in doc:
    text += page.get_text()
print(text)
doc.close()