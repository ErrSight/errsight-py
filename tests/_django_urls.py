from django.http import HttpResponse
from django.urls import path


def hello(request):
    return HttpResponse("ok")


urlpatterns = [path("hello/", hello, name="hello-view")]
