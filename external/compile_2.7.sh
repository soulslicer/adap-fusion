sudo apt-get install libpng++-dev libpcl-dev libproj-dev libopencv-dev
sudo ln -sfn /usr/lib/x86_64-linux-gnu/libEGL.so.1.1.0  /usr/lib/x86_64-linux-gnu/libEGL.so
sudo ln -sfn /usr/lib/x86_64-linux-gnu/libGL.so.1.7.0  /usr/lib/x86_64-linux-gnu/libGL.so
sudo ln -s /usr/lib/x86_64-linux-gnu/libvtkCommonCore-6.2.so /usr/lib/libvtkproj4.so
cd deval_lib
rm -rf build; mkdir build; cd build;
cmake -DCMAKE_BUILD_TYPE=Release -DPYTHON_EXECUTABLE=/usr/bin/python ..
make -j4
cp pyevaluatedepth_lib.so ../
cd ..; cd ..
cd perception_lib
rm -rf build; mkdir build; cd build;
cmake -DCMAKE_BUILD_TYPE=Release -DPYTHON_EXECUTABLE=/usr/bin/python ..
make -j4
cp pyperception_lib.so ../
cp libperception_lib.so ../
cd ..; cd ..
cd utils_lib
rm -rf build; mkdir build; cd build;
cmake -DCMAKE_BUILD_TYPE=Release -DPYTHON_EXECUTABLE=/usr/bin/python ..
make -j4
cp utils_lib.so ../
cd ..; cd ..
cd lcsim
git submodule init
git submodule update
git checkout raaj_temp
rm -rf build; mkdir build; cd build;
cmake -DCMAKE_BUILD_TYPE=Release -DPYTHON_EXECUTABLE=/usr/bin/python -DPYTHON_INCLUDE_DIR=/usr/include/python2.7 -DPYTHON_LIBRARY=/usr/lib/x86_64-linux-gnu/libpython2.7.so ..
make -j4
cd ..; cd ..;
