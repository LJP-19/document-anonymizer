"""Build the fictional demo organizer (every name and number is invented).

    python demo/make_demo.py demo/Feldman_2025_Organizer.pdf
    DOCANON_LLM=0 python -m app.cli process demo/Feldman_2025_Organizer.pdf --accept-all -o demo/out.pdf
"""
import pymupdf, sys
GRAY=(0.25,0.25,0.25); BLK=(0,0,0)
doc=pymupdf.open(); p=doc.new_page(width=612,height=792)
def L(x,y,t,s=8): p.insert_text((x,y),t,fontsize=s,fontname="helv",color=GRAY)
def V(x,y,t,s=10): p.insert_text((x,y),t,fontsize=s,fontname="cour",color=BLK)
p.insert_text((40,50),"2025 Client Tax Organizer - Personal Information",fontsize=14,fontname="hebo",color=GRAY)
L(40,66,"Please review and correct any information below. Return with your documents.")
L(40,95,"Taxpayer name"); V(40,108,"Marcus T. Feldman")
L(300,95,"Taxpayer SSN"); V(300,108,"512-48-7391")
L(40,130,"Spouse name"); V(40,143,"Elena R. Feldman")
L(300,130,"Spouse SSN"); V(300,143,"528-61-0943")
L(40,165,"Home address (number and street)"); V(40,178,"4817 Larkspur Ridge Dr")
L(40,198,"City, state and ZIP code"); V(40,211,"Fremont, CA 94539")
L(300,165,"Daytime phone"); V(300,178,"(510) 555-0142")
L(300,198,"Email address"); V(300,211,"mfeldman.home@example.com")
L(40,240,"Dependent:"); V(95,240,"Dương Thị Hồng")
L(300,240,"Relationship"); V(370,240,"Daughter")
L(40,262,"Date of birth"); V(120,262,"03/14/2011")
p.insert_text((40,300),"Income Summary",fontsize=11,fontname="hebo",color=GRAY)
rows=[("Wages, salaries, tips (W-2, box 1)","$612,450.00"),("Taxable interest","$18,322.17"),
      ("Qualified dividends","$41,905.00"),("Long-term capital gain","$128,600.00")]
y=320
for a,b in rows: L(40,y,a,9); V(420,y,b); y+=18
p.insert_text((40,420),"Signature block",fontsize=11,fontname="hebo",color=GRAY)
V(40,440,"Marcus & Elena Feldman")
L(40,470,"Mail estimated payments to:"); L(40,482,"Internal Revenue Service"); L(40,494,"P.O. Box 931000"); L(40,506,"Louisville, KY 40293-1000")
L(40,760,"Organizer page 1 of 1 - prepared for Marcus Feldman")
doc.save(sys.argv[1])
