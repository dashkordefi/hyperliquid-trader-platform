from django.db import models


class PersonRecord(models.Model):
    last_name = models.CharField("Фамилия", max_length=120)
    first_name = models.CharField("Имя", max_length=120)
    age = models.PositiveIntegerField("Возраст")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Запись"
        verbose_name_plural = "Записи"

    def __str__(self) -> str:
        return f"{self.last_name} {self.first_name} ({self.age})"
