from rest_framework import serializers


class RouteRequestSerializer(serializers.Serializer):
    start = serializers.CharField(
        max_length=300,
        trim_whitespace=True,
    )
    finish = serializers.CharField(
        max_length=300,
        trim_whitespace=True,
    )

    def validate(self, attrs):
        if attrs["start"].casefold() == attrs["finish"].casefold():
            raise serializers.ValidationError(
                "Start and finish locations must be different."
            )
        return attrs