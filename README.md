# Mask Annotator

A lightweight PySide6 desktop app for drawing and correcting binary segmentation masks.

## Start

```bash
python -m pip install -r requirements.txt
python app.py
```

## Use

1. Arrange your project folder like this:

   ```text
   my-project/
   ├── images/   # original input images
   └── masks/    # input masks, named with the same image stem
   ```

2. Choose **Open project folder** and select `my-project`. The app loads every supported image in `images/` and looks for its matching mask in `masks/`. Missing masks start as blank, black masks.
3. Select **Working as** in the top toolbar. Use **Edit users** to add the list of annotator names. A username is required before mask editing is enabled.
4. Paint either directly on the original image at left (recommended for visual context) or on the mask at right. Both use the same brush: **Paint white** adds foreground and **Erase to black** removes it. Every stroke updates the mask immediately and can be undone.
5. Files are autosaved about half a second after an edit into `my-project/output/masks`. The original `images/` and `masks/` folders are never changed.

The edited binary mask is saved as `<image-stem>.png` in `output/masks`. On completion, the original image is copied to `output/images` with the same name. On future sessions, the tool reloads the working mask from `output/masks`, so any user can continue editing it while the original mask remains available.

Click **Save + next** when an annotation is finished. It saves the mask, copies the image and mask into `output/`, moves the image from the left-side **Incomplete** list to **Completed**, then opens the next image automatically. Completion data for every image is saved together in one file: `my-project/completed/completed.json`, keyed by image name. **Mark incomplete** removes that image from the JSON list but retains its working output for further edits. **Delete saved output** removes only the output image/mask and completion status; source images and masks remain safe.

The chosen project path and user list are saved in [config.json](config.json), next to the app. On the next launch, that project and the last selected user are restored automatically. The project root also receives `annotation_progress.json`, recording each edited image, its contributors, latest editor, and edit time. The left panel shows completion status and the number of unique images edited by each user.

## Shortcuts

- `B` — paint white
- `E` — erase to black
- `Ctrl/Cmd + Z` — undo
- `Ctrl/Cmd + Shift + Z` — redo
- `Ctrl/Cmd + S` — save immediately
- Left/Right arrow — previous/next image
