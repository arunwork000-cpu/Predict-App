import mimetypes

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible


@deconstructible
class DatabaseStorage(Storage):
    """Keep uploaded files (team flags) in the database.

    Railway's container disk is ephemeral and /media/ has no web server in
    front of it, so files written to disk vanish on every deploy. Rows in
    Postgres survive, and are served back by predictions.views.media_file.
    """

    def _model(self):
        from .models import StoredFile

        return StoredFile

    def _open(self, name, mode="rb"):
        stored = self._model().objects.get(name=name)
        return ContentFile(bytes(stored.content), name=name)

    def _save(self, name, content):
        data = content.read()
        content_type = (
            getattr(content, "content_type", None)
            or mimetypes.guess_type(name)[0]
            or "application/octet-stream"
        )
        self._model().objects.update_or_create(
            name=name,
            defaults={
                "content": data,
                "content_type": content_type,
                "size": len(data),
            },
        )
        return name

    def exists(self, name):
        return self._model().objects.filter(name=name).exists()

    def delete(self, name):
        self._model().objects.filter(name=name).delete()

    def size(self, name):
        return self._model().objects.values_list("size", flat=True).get(name=name)

    def url(self, name):
        return settings.MEDIA_URL + name.lstrip("/")

    def listdir(self, path):
        prefix = path.strip("/")
        prefix = f"{prefix}/" if prefix else ""
        files = [
            n[len(prefix):]
            for n in self._model()
            .objects.filter(name__startswith=prefix)
            .values_list("name", flat=True)
            if "/" not in n[len(prefix):]
        ]
        return [], files
