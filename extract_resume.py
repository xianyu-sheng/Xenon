from pdfminer.high_level import extract_text
text = extract_text(r'D:\工作\AI_Agent简历.pdf')
with open('temp_resume.txt', 'w', encoding='utf-8') as f:
    f.write(text)
print('提取完成')