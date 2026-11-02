using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.IO;

static class IconBuilder
{
    static GraphicsPath Rounded(float x, float y, float w, float h, float radius)
    {
        float d = radius * 2;
        var path = new GraphicsPath();
        path.AddArc(x, y, d, d, 180, 90);
        path.AddArc(x + w - d, y, d, d, 270, 90);
        path.AddArc(x + w - d, y + h - d, d, d, 0, 90);
        path.AddArc(x, y + h - d, d, d, 90, 90);
        path.CloseFigure();
        return path;
    }

    static void DrawRect(Graphics g, Color color, float x, float y, float w, float h, float r)
    {
        using (var path = Rounded(x, y, w, h, r))
        using (var brush = new SolidBrush(color)) g.FillPath(brush, path);
    }

    static void Main(string[] args)
    {
        string output = Path.GetFullPath(args[0]);
        Directory.CreateDirectory(output);
        int[] sizes = { 16, 20, 24, 32, 40, 48, 64, 128, 256 };
        using (var master = new Bitmap(1024, 1024, PixelFormat.Format32bppArgb))
        {
            using (var g = Graphics.FromImage(master))
            {
                g.Clear(Color.Transparent);
                g.SmoothingMode = SmoothingMode.AntiAlias;
                g.ScaleTransform(8, 8);
                g.TranslateTransform(64, 64);
                g.RotateTransform(-5);
                g.TranslateTransform(-64, -64);
                using (var path = Rounded(9, 9, 110, 110, 33))
                using (var fill = new SolidBrush(ColorTranslator.FromHtml("#1c2929")))
                using (var stroke = new Pen(ColorTranslator.FromHtml("#7da995"), 3))
                {
                    g.FillPath(fill, path);
                    g.DrawPath(stroke, path);
                }
                DrawRect(g, ColorTranslator.FromHtml("#c7ead7"), 36, 42, 11, 45, 5.5f);
                DrawRect(g, ColorTranslator.FromHtml("#83c1a3"), 56, 54, 11, 33, 5.5f);
                DrawRect(g, ColorTranslator.FromHtml("#568f76"), 76, 63, 11, 24, 5.5f);
            }
            var images = new List<byte[]>();
            foreach (int size in sizes)
            {
                using (var image = new Bitmap(size, size, PixelFormat.Format32bppArgb))
                using (var g = Graphics.FromImage(image))
                using (var stream = new MemoryStream())
                {
                    g.CompositingMode = CompositingMode.SourceCopy;
                    g.InterpolationMode = InterpolationMode.HighQualityBicubic;
                    g.SmoothingMode = SmoothingMode.HighQuality;
                    g.PixelOffsetMode = PixelOffsetMode.HighQuality;
                    g.DrawImage(master, 0, 0, size, size);
                    image.Save(stream, ImageFormat.Png);
                    if (size == 256) image.Save(Path.Combine(output, "studiowright-icon-preview.png"), ImageFormat.Png);
                    images.Add(stream.ToArray());
                }
            }
            using (var stream = new FileStream(Path.Combine(output, "studiowright.ico"), FileMode.CreateNew))
            using (var writer = new BinaryWriter(stream))
            {
                writer.Write((ushort)0); writer.Write((ushort)1); writer.Write((ushort)sizes.Length);
                int offset = 6 + sizes.Length * 16;
                for (int i = 0; i < sizes.Length; i++)
                {
                    writer.Write((byte)(sizes[i] == 256 ? 0 : sizes[i]));
                    writer.Write((byte)(sizes[i] == 256 ? 0 : sizes[i]));
                    writer.Write((byte)0); writer.Write((byte)0);
                    writer.Write((ushort)1); writer.Write((ushort)32);
                    writer.Write(images[i].Length); writer.Write(offset);
                    offset += images[i].Length;
                }
                foreach (byte[] image in images) writer.Write(image);
            }
        }
        using (var icon = new Icon(Path.Combine(output, "studiowright.ico")))
            Console.WriteLine("Icon ready: " + icon.Width + "x" + icon.Height);
    }
}
