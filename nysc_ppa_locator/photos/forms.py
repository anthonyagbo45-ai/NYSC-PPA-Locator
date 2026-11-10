from uuid import uuid4

from django import forms


class PhotoForm(forms.Form):
    image = forms.FileField(
        label='Photo',
        widget=forms.ClearableFileInput(attrs={'accept': 'image/jpeg,image/png,image/webp'}),
        help_text='One still JPEG, PNG or WebP, up to 8 MB and 16 million pixels. Animated images and documents are not accepted.',
    )
    caption = forms.CharField(
        max_length=500, label='Describe this photo',
        widget=forms.Textarea(attrs={'rows': 3}),
        help_text='A short description makes the photo useful without seeing it. Keep personal identifiers and private addresses out.',
    )
    client_token = forms.UUIDField(widget=forms.HiddenInput, initial=uuid4)
    rights_confirmed = forms.BooleanField(
        label='I have permission to share this photo and it contains no identity documents, credentials or private information about other people.',
    )
