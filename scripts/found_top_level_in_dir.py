import zipfile
import glob

wheel = glob.glob("*.whl")[0]

with zipfile.ZipFile(wheel) as z:
    files = z.namelist()

    for f in files:
        if "top_level.txt" in f:
            print("מצאתי:", f)
            print(z.read(f).decode())
            break
    else:
        print("אין top_level.txt")