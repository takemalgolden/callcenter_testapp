import unittest,sys,os,io,csv,zipfile
sys.path.insert(0,os.path.dirname(os.path.dirname(__file__)))
from server import parse_employee_file
class ImportTest(unittest.TestCase):
 def test_csv(self):
  data='ユーザー名,表示名,パスワード,スキル,勤務開始,勤務終了\nnew001,山田太郎,Example123!,"Consumer,PP",09:00,18:00\n'.encode('utf-8-sig')
  rows=parse_employee_file('staff.csv',data)
  self.assertEqual(rows[0][3],['Consumer','PP'])
 def test_xlsx(self):
  xml='''<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1">'''
  for i,v in enumerate(['ユーザー名','表示名','パスワード','スキル','勤務開始','勤務終了']):
   xml+=f'<c r="{chr(65+i)}1" t="inlineStr"><is><t>{v}</t></is></c>'
  xml+='</row><row r="2">'
  for i,v in enumerate(['new002','鈴木','Example123!','Merchant','10:00','19:00']):
   xml+=f'<c r="{chr(65+i)}2" t="inlineStr"><is><t>{v}</t></is></c>'
  xml+='</row></sheetData></worksheet>'
  stream=io.BytesIO()
  with zipfile.ZipFile(stream,'w') as z:z.writestr('xl/worksheets/sheet1.xml',xml)
  self.assertEqual(parse_employee_file('staff.xlsx',stream.getvalue())[0][0],'new002')
 def test_invalid(self):
  data='ユーザー名,表示名,パスワード,スキル,勤務開始,勤務終了\nx,氏名,weak,PP,09:00,18:00\n'.encode()
  with self.assertRaises(ValueError):parse_employee_file('a.csv',data)
if __name__=='__main__':unittest.main()
