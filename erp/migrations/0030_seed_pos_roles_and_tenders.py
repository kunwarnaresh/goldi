from django.db import migrations

TENDERS = [
    # code, name, type, requires_reference, allow_change, allow_refund
    ('CASH', 'Cash', 'cash', False, True, True),
    ('CC', 'Credit Card', 'card', True, False, True),
    ('DC', 'Debit Card', 'card', True, False, True),
    ('UPI', 'UPI', 'digital', True, False, True),
    ('BANK', 'Bank Transfer', 'bank', True, False, True),
    ('CHEQUE', 'Cheque', 'cheque', True, False, False),
    ('GIFT', 'Gift Card', 'gift_card', True, False, False),
    ('CREDIT', 'Store Credit', 'store_credit', False, False, False),
    ('WALLET', 'Wallet', 'wallet', True, False, True),
]


def seed(apps, schema_editor):
    from erp.retail.permissions import DEFAULT_ROLES
    POSRole = apps.get_model('erp', 'POSRole')
    Tender = apps.get_model('erp', 'Tender')
    for code, name, base_role, pos_access, permissions in DEFAULT_ROLES:
        POSRole.objects.get_or_create(code=code, defaults={
            'name': name, 'base_role': base_role, 'pos_access': pos_access, 'permissions': permissions, 'is_system': True})
    for code, name, tender_type, reference, change, refund in TENDERS:
        Tender.objects.get_or_create(code=code, defaults={
            'name': name, 'tender_type': tender_type, 'requires_reference': reference, 'allow_change': change,
            'allow_refund': refund})


class Migration(migrations.Migration):
    dependencies = [('erp', '0029_store_pos_tender_architecture')]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
