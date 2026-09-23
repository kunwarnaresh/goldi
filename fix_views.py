import re

with open('c:\\myproject\\goldi\\erp\\views.py', 'r') as f:
    content = f.read()

# Find the gst_delete function return statement and truncate there
pattern = r"(def gst_delete\(request, pk\):.*?return render\(request, 'erp/gst_confirm_delete\.html', \{'gst': gst\}\))"
match = re.search(pattern, content, re.DOTALL)

if match:
    clean_content = content[:match.end()]
    with open('c:\\myproject\\goldi\\erp\\views.py', 'w') as f:
        f.write(clean_content)
    print("File truncated successfully")
else:
    print("Could not find gst_delete function")
