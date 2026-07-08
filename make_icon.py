"""Generate icon.ico — dark tile with the mp4trim timeline motif."""

from PIL import Image, ImageDraw

S = 256


def rounded(d, box, r, fill):
    d.rounded_rectangle(box, radius=r, fill=fill)


img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

rounded(d, (8, 8, S - 8, S - 8), 52, (20, 20, 24, 255))          # tile
rounded(d, (36, 108, S - 36, 148), 14, (42, 42, 47, 255))        # track
rounded(d, (78, 108, S - 78, 148), 0, (46, 125, 50, 255))        # kept region
rounded(d, (66, 84, 92, 172), 12, (102, 187, 106, 255))          # in handle
rounded(d, (S - 92, 84, S - 66, 172), 12, (239, 83, 80, 255))    # out handle
rounded(d, (124, 64, 132, 192), 4, (240, 240, 242, 255))         # playhead
d.polygon([(114, 56), (142, 56), (128, 76)], fill=(240, 240, 242, 255))

img.save("icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                            (64, 64), (128, 128), (256, 256)])
img.save("icon.png")
print("icon.ico written")
