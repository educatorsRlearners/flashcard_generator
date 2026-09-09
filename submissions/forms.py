from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator

_validate_url = URLValidator(schemes=["http", "https"])


class URLSubmissionForm(forms.Form):
    urls = forms.CharField(
        label="URLs",
        required=False,
        widget=forms.Textarea(
            attrs={"rows": 10, "placeholder": "One URL per line"}
        ),
    )

    def clean_urls(self):
        raw = self.cleaned_data.get("urls", "") or ""
        lines = [line.strip() for line in raw.splitlines()]
        non_empty = [line for line in lines if line]

        if not non_empty:
            raise ValidationError("Please enter at least one URL.")

        valid_urls = []
        invalid_lines = []
        seen = set()

        for line in non_empty:
            try:
                _validate_url(line)
            except ValidationError:
                invalid_lines.append(line)
                continue
            if line in seen:
                continue
            seen.add(line)
            valid_urls.append(line)

        self.valid_urls = valid_urls
        self.invalid_lines = invalid_lines
        return raw
