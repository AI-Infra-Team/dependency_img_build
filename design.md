- we want to take over the docker build process with dynamicly generated dockerfiles and cache images at multi-level (ghcr, local-area-net minio)
- description part
    - user can only decalar what they need in the image
        - with user, sudo and 
        - apt/yum packs
        - dynamic env scripts
    - user need to calrify stage for installs with dependency
        - we only garantee install in different stage will be done in order
- build part, system will record the old build steps, so if new changes come, it will only rebuild the one matches come and after them
  [keep,keep,keep, rebuild(new change), rebuild, rebuild..]