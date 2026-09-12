from django.db import models

class State(models.Model):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class LGA(models.Model):
    state = models.ForeignKey(State, on_delete=models.CASCADE, related_name="lgas")
    name = models.CharField(max_length=100)

    class Meta:
        ordering = ['name']
        unique_together = ('state', 'name')

    def __str__(self):
        return f"{self.name} ({self.state.name})"