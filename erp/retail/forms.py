from django import forms
from django.core.exceptions import ValidationError

from erp.models import (
    Location, POSRole, POSStaff, POSStaffAssignment, POSTerminal, Store, StoreTender, Tender,
)

from . import services
from .permissions import CATALOG

class DateInput(forms.DateInput):
    input_type = 'date'


def _dates(form, *names):
    for name in names:
        if name in form.fields:
            form.fields[name].widget = DateInput(format='%Y-%m-%d')


class LocationForm(forms.ModelForm):
    class Meta:
        model = Location
        fields = ['company', 'branch', 'warehouse', 'location_code', 'location_name', 'address', 'city', 'state_code',
                  'country', 'status']
        widgets = {'address': forms.Textarea(attrs={'rows': 2})}

    SECTIONS = [('General', ['company', 'location_code', 'location_name', 'status']),
                ('Structure', ['branch', 'warehouse']),
                ('Address', ['address', 'city', 'state_code', 'country'])]


class StoreForm(forms.ModelForm):
    class Meta:
        model = Store
        fields = ['company', 'code', 'name', 'store_type', 'status', 'location', 'branch', 'address', 'city', 'state',
                  'country', 'pin_code', 'gstin', 'phone', 'email', 'manager', 'opening_date', 'closing_date']
        widgets = {'address': forms.Textarea(attrs={'rows': 2})}

    SECTIONS = [('General', ['company', 'code', 'name', 'store_type', 'status']),
                ('Location', ['location', 'branch']),
                ('Address & contact', ['address', 'city', 'state', 'country', 'pin_code', 'gstin', 'phone', 'email']),
                ('Management', ['manager', 'opening_date', 'closing_date'])]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _dates(self, 'opening_date', 'closing_date')
        self.fields['location'].required = True
        # Only locations that are free, plus this store's own.
        free = Location.objects.filter(store__isnull=True)
        if self.instance.pk and self.instance.location_id:
            free = Location.objects.filter(pk=self.instance.location_id) | free
        self.fields['location'].queryset = free.select_related('company').order_by('location_code')
        self.fields['location'].help_text = 'One location = one store. Locations already used by another store are not listed.'
        self.fields['manager'].queryset = (POSStaff.objects.filter(store=self.instance, is_active=True)
                                           if self.instance.pk else POSStaff.objects.none())
        if not self.instance.pk:
            self.fields['manager'].help_text = 'Available once the store has staff.'

    def clean(self):
        data = super().clean()
        location = data.get('location')
        if location is not None:
            self.instance.company = data.get('company') or self.instance.company
            try:
                services.validate_store_location(self.instance, location)
            except ValidationError as exc:
                for field, errors in exc.error_dict.items():
                    self.add_error(field, errors)
        return data


class StaffForm(forms.ModelForm):
    location = forms.ModelChoiceField(queryset=Location.objects.filter(store__isnull=False).order_by('location_code'),
                                      required=False, help_text='Selecting a location selects its store.')
    password = forms.CharField(widget=forms.PasswordInput(render_value=False), required=False, strip=False)
    confirm_password = forms.CharField(widget=forms.PasswordInput(render_value=False), required=False, strip=False)

    class Meta:
        model = POSStaff
        fields = ['employee_code', 'name', 'first_name', 'last_name', 'mobile', 'email', 'designation', 'department',
                  'store', 'staff_role', 'default_terminal', 'pos_access', 'login_id', 'is_active',
                  'password_change_required', 'joining_date', 'leaving_date']
        labels = {'staff_role': 'Role', 'is_active': 'Active', 'login_id': 'Login ID', 'name': 'Staff name'}

    SECTIONS = [('General', ['employee_code', 'name', 'first_name', 'last_name', 'mobile', 'email', 'designation', 'department']),
                ('Organization', ['location', 'store', 'staff_role', 'is_active', 'joining_date', 'leaving_date']),
                ('POS access', ['pos_access', 'login_id', 'password', 'confirm_password', 'password_change_required',
                                'default_terminal'])]

    def __init__(self, *args, fixed_store=None, **kwargs):
        super().__init__(*args, **kwargs)
        _dates(self, 'joining_date', 'leaving_date')
        self.fixed_store = fixed_store
        self.fields['store'].queryset = Store.objects.filter(location__isnull=False).select_related('location').order_by('code')
        self.fields['store'].required = False  # derived from location when blank; enforced in clean()
        self.fields['staff_role'].queryset = POSRole.objects.filter(is_active=True)
        self.fields['staff_role'].required = True
        store = fixed_store or (self.instance.store if self.instance.pk else None)
        self.fields['default_terminal'].queryset = (POSTerminal.objects.filter(store=store) if store else POSTerminal.objects.none())
        self.fields['default_terminal'].help_text = 'Must be a terminal of the staff member\'s store.'
        if store is not None:
            self.initial.setdefault('store', store.pk)
            self.initial.setdefault('location', store.location_id)
        if fixed_store is not None:
            for name in ('store', 'location'):
                self.fields[name].disabled = True
        if self.instance.pk:
            self.fields['password'].help_text = 'Leave blank to keep the current password. Use Reset Password to change it.'
            self.fields['password'].widget = forms.HiddenInput()
            self.fields['confirm_password'].widget = forms.HiddenInput()
        else:
            self.fields['password'].help_text = f'At least {services.PASSWORD_MIN_LENGTH} characters. Stored only as a salted hash.'
        # Store options carry their location so the page can keep the two selectors in step.
        self.store_locations = {s.pk: s.location_id for s in self.fields['store'].queryset}

    def clean_login_id(self):
        login_id = (self.cleaned_data.get('login_id') or '').strip().lower() or None
        if login_id and POSStaff.objects.filter(login_id=login_id).exclude(pk=self.instance.pk).exists():
            raise ValidationError('This login ID is already used by another staff member.')
        return login_id

    def clean(self):
        data = super().clean()
        store = self.fixed_store or data.get('store')
        location = self.fixed_store.location if self.fixed_store else data.get('location')
        if store is None and location is not None:
            store = services.resolve_store_for_location(location)
        if location is None and store is not None:
            location = store.location
        if store is None:
            self.add_error('store', 'Select a store (or a location that has a store).')
            return data
        data['store'], data['location'] = store, location
        self.instance.store = store
        try:
            self.warnings = services.validate_staff_placement(
                POSStaff.objects.get(pk=self.instance.pk) if self.instance.pk else self.instance, store, location)
        except ValidationError as exc:
            for field, errors in exc.error_dict.items():
                self.add_error(field if field in self.fields else None, errors)
        terminal = data.get('default_terminal')
        if terminal is not None and terminal.store_id != store.pk:
            self.add_error('default_terminal', 'Default POS terminal must belong to the selected store.')
        if data.get('pos_access'):
            if not data.get('login_id'):
                self.add_error('login_id', 'Login ID is required when POS login is enabled.')
            if not self.instance.pk and not data.get('password'):
                self.add_error('password', 'Password is required when POS login is enabled.')
        if data.get('password') or data.get('confirm_password'):
            try:
                services.validate_password_value(data.get('password'), data.get('confirm_password'))
            except ValidationError as exc:
                self.add_error('password', exc)
        return data


class PasswordResetForm(forms.Form):
    password = forms.CharField(widget=forms.PasswordInput, strip=False)
    confirm_password = forms.CharField(widget=forms.PasswordInput, strip=False)
    require_change = forms.BooleanField(required=False, initial=True, label='Require change at next login')

    def clean(self):
        data = super().clean()
        try:
            services.validate_password_value(data.get('password'), data.get('confirm_password'))
        except ValidationError as exc:
            self.add_error('password', exc)
        return data


class TerminalForm(forms.ModelForm):
    class Meta:
        model = POSTerminal
        fields = ['store', 'code', 'name', 'terminal_type', 'status', 'active_from', 'active_to', 'device_id',
                  'serial_number', 'ip_address', 'mac_address', 'printer', 'receipt_printer', 'auto_print', 'cash_drawer',
                  'barcode_scanner', 'customer_display', 'payment_device', 'default_tender']
        labels = {'code': 'Terminal code', 'name': 'Terminal name'}

    SECTIONS = [('General', ['store', 'code', 'name', 'terminal_type', 'status', 'active_from', 'active_to']),
                ('Device', ['device_id', 'serial_number', 'ip_address', 'mac_address']),
                ('Peripherals', ['printer', 'receipt_printer', 'auto_print', 'cash_drawer', 'barcode_scanner',
                                 'customer_display', 'payment_device']),
                ('Payment', ['default_tender'])]

    def __init__(self, *args, fixed_store=None, **kwargs):
        super().__init__(*args, **kwargs)
        _dates(self, 'active_from', 'active_to')
        self.fixed_store = fixed_store
        self.fields['store'].queryset = Store.objects.filter(location__isnull=False).select_related('location').order_by('code')
        if fixed_store is not None:
            self.initial['store'] = fixed_store.pk
            self.fields['store'].disabled = True
        store = fixed_store or (self.instance.store if self.instance.pk else None)
        self.fields['default_tender'].queryset = (Tender.objects.filter(store_tenders__store=store, store_tenders__active=True)
                                                  if store else Tender.objects.none())
        self.fields['default_tender'].help_text = 'One of the store\'s active tenders.'
        self.store_locations = {s.pk: str(s.location) for s in self.fields['store'].queryset}

    def clean_code(self):
        return (self.cleaned_data.get('code') or '').strip().upper()

    def clean(self):
        data = super().clean()
        store = self.fixed_store or data.get('store')
        if store is None:
            self.add_error('store', 'A POS terminal must belong to a store.')
            return data
        if store.location_id is None:
            self.add_error('store', f'Store {store.code} has no Location assigned.')
        if not self.instance.pk and store.status != 'active':
            self.add_error('store', f'Store {store.code} is not active.')
        code = data.get('code')
        if code and POSTerminal.objects.filter(store=store, code=code).exclude(pk=self.instance.pk).exists():
            self.add_error('code', f'Terminal code {code} already exists in store {store.code}.')
        self.instance.store = store
        return data


class TenderForm(forms.ModelForm):
    class Meta:
        model = Tender
        fields = ['code', 'name', 'tender_type', 'status', 'description', 'currency', 'payment_gateway', 'payment_method',
                  'gl_account', 'bank_account', 'requires_reference', 'requires_approval', 'allow_refund', 'allow_change',
                  'allow_split_payment', 'allow_partial_payment', 'minimum_amount', 'maximum_amount']
        widgets = {'description': forms.Textarea(attrs={'rows': 2})}

    SECTIONS = [('General', ['code', 'name', 'tender_type', 'status', 'currency', 'description']),
                ('Posting', ['payment_gateway', 'payment_method', 'gl_account', 'bank_account']),
                ('Rules', ['requires_reference', 'requires_approval', 'allow_refund', 'allow_change', 'allow_split_payment',
                           'allow_partial_payment', 'minimum_amount', 'maximum_amount'])]

    def clean_code(self):
        return (self.cleaned_data.get('code') or '').strip().upper()


class StoreTenderForm(forms.ModelForm):
    class Meta:
        model = StoreTender
        fields = ['tender', 'active', 'is_default', 'sequence', 'requires_reference', 'allow_refund', 'allow_change',
                  'allow_split_payment', 'minimum_amount', 'maximum_amount', 'gl_account', 'effective_from', 'effective_to']
        labels = {'is_default': 'Default tender'}

    SECTIONS = [('Tender', ['tender', 'active', 'is_default', 'sequence']),
                ('Store rules', ['requires_reference', 'allow_refund', 'allow_change', 'allow_split_payment',
                                 'minimum_amount', 'maximum_amount', 'gl_account', 'effective_from', 'effective_to'])]

    def __init__(self, *args, store=None, **kwargs):
        super().__init__(*args, **kwargs)
        _dates(self, 'effective_from', 'effective_to')
        self.store = store or self.instance.store
        self.fields['sequence'].required = False
        if self.instance.pk:
            self.fields['tender'].disabled = True
        else:
            taken = StoreTender.objects.filter(store=self.store).values_list('tender_id', flat=True)
            self.fields['tender'].queryset = Tender.objects.filter(status='active').exclude(pk__in=list(taken))
            self.fields['tender'].help_text = 'Active tenders not yet assigned to this store.'
            # Default the store rules to the tender master's rules; the page copies them over on selection.
            self.fields['allow_split_payment'].initial = True
            self.fields['allow_refund'].initial = True
        self.tender_rules = {t.pk: {'requires_reference': t.requires_reference, 'allow_refund': t.allow_refund,
                                    'allow_change': t.allow_change, 'allow_split_payment': t.allow_split_payment}
                             for t in self.fields['tender'].queryset}

    def clean_sequence(self):
        sequence = self.cleaned_data.get('sequence')
        return 10 if sequence is None else sequence

    def clean(self):
        data = super().clean()
        tender = data.get('tender')
        if tender is not None and not self.instance.pk:
            if tender.status != 'active':
                self.add_error('tender', 'Only active tenders can be assigned.')
            elif StoreTender.objects.filter(store=self.store, tender=tender).exists():
                self.add_error('tender', f'{tender.name} is already assigned to {self.store.name}.')
        self.instance.store = self.store
        return data


class AssignmentForm(forms.ModelForm):
    class Meta:
        model = POSStaffAssignment
        fields = ['terminal', 'role', 'active', 'primary_terminal', 'effective_from', 'effective_to']
        labels = {'primary_terminal': 'Default terminal'}

    def __init__(self, *args, staff=None, **kwargs):
        super().__init__(*args, **kwargs)
        _dates(self, 'effective_from', 'effective_to')
        self.staff = staff or self.instance.staff
        self.fields['terminal'].queryset = POSTerminal.objects.filter(store_id=self.staff.store_id).order_by('code')
        self.fields['terminal'].help_text = 'Only terminals of the staff member\'s store can be assigned.'
        self.initial.setdefault('role', self.staff.role)

    def clean(self):
        data = super().clean()
        terminal = data.get('terminal')
        if terminal is not None:
            if terminal.store_id != self.staff.store_id:
                self.add_error('terminal', 'Staff can only be assigned to POS terminals of their own store.')
            elif POSStaffAssignment.objects.filter(staff=self.staff, terminal=terminal, role=data.get('role')).exclude(pk=self.instance.pk).exists():
                self.add_error('terminal', 'This staff member is already assigned to that terminal with this role.')
        self.instance.staff = self.staff
        return data


class POSRoleForm(forms.ModelForm):
    permissions = forms.MultipleChoiceField(choices=[(code, f'{group}: {label}') for code, label, group in CATALOG],
                                            widget=forms.CheckboxSelectMultiple, required=False)

    class Meta:
        model = POSRole
        fields = ['code', 'name', 'base_role', 'pos_access', 'is_active', 'description', 'permissions']
        widgets = {'description': forms.Textarea(attrs={'rows': 2})}
        labels = {'base_role': 'Role category', 'pos_access': 'Allows POS login'}

    SECTIONS = [('Role', ['code', 'name', 'base_role', 'pos_access', 'is_active', 'description']),
                ('Permissions', ['permissions'])]

    def clean_code(self):
        return (self.cleaned_data.get('code') or '').strip().upper()


class ShiftCloseForm(forms.Form):
    actual_cash = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0, label='Counted cash')
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 2}))


class ImportUploadForm(forms.Form):
    import_type = forms.ChoiceField(choices=[])
    file = forms.FileField(help_text='.xlsx workbook based on the downloaded template.')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from erp.models import RetailImportBatch
        self.fields['import_type'].choices = RetailImportBatch.TYPES
