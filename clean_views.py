with open('c:\\myproject\\goldi\\erp\\views.py', 'r') as f:
    lines = f.readlines()

# Find where the corruption starts - look for the gst_confirm_delete line
clean_lines = []
for i, line in enumerate(lines):
    clean_lines.append(line)
    # Stop at the gst_delete return statement
    if 'gst_confirm_delete.html' in line and i > 630:
        break

# Write back the clean version
with open('c:\\myproject\\goldi\\erp\\views.py', 'w') as f:
    f.writelines(clean_lines)

print(f"File cleaned to {len(clean_lines)} lines")
